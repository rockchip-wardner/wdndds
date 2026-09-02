/* wdndds_cshim.cpp — extern "C" wrapper around FastDDS 3.6.
 *
 * Uses the rrpc API (eprosima::fastdds::dds::rpc::Service /
 * Replier / Requester) on top of IDL-generated ComputeRequest /
 * ComputeReply types. Topics are auto-named by FastDDS:
 *   request topic = "<service_name>_Request"
 *   reply   topic = "<service_name>_Reply"
 * Correlation is handled end-to-end via FastDDS SampleIdentity; the
 * IDL `request_id` field is kept for application-level correlation /
 * logging only.
 *
 * Transport: SHM (local) + UDPv4 (LAN) + TCPv4 (cross-subnet), all
 * configured by FastDDS built-in transports (`use_builtin_transports=true`).
 *
 * Discovery:
 *   - Default (no discovery_servers_csv): SIMPLE multicast + initialPeers.
 *   - With discovery_servers_csv + is_discovery_server=1: SERVER
 *     protocol on a FastDDS discovery daemon.
 *   - With discovery_servers_csv + is_discovery_server=0: CLIENT
 *     protocol pointing at the daemon.
 */
#include "wdndds_cshim.h"

#include <atomic>
#include <chrono>
#include <cstring>
#include <memory>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <mutex>

#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/dds/domain/qos/DomainParticipantQos.hpp>
#include <fastdds/dds/domain/qos/ReplierQos.hpp>
#include <fastdds/dds/domain/qos/RequesterQos.hpp>
#include <fastdds/dds/publisher/Publisher.hpp>
#include <fastdds/dds/publisher/DataWriter.hpp>
#include <fastdds/dds/publisher/qos/DataWriterQos.hpp>
#include <fastdds/dds/subscriber/Subscriber.hpp>
#include <fastdds/dds/subscriber/DataReader.hpp>
#include <fastdds/dds/subscriber/qos/DataReaderQos.hpp>
#include <fastdds/dds/topic/Topic.hpp>
#include <fastdds/dds/topic/TopicDescription.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>
#include <fastdds/dds/core/LoanableSequence.hpp>
#include <fastdds/dds/domain/DomainParticipantListener.hpp>
#include <fastdds/dds/publisher/DataWriterListener.hpp>
#include <fastdds/dds/subscriber/DataReaderListener.hpp>
#include <fastdds/dds/rpc/Service.hpp>
#include <fastdds/dds/rpc/Replier.hpp>
#include <fastdds/dds/rpc/Requester.hpp>
#include <fastdds/dds/rpc/ServiceTypeSupport.hpp>
#include <fastdds/dds/rpc/RequestInfo.hpp>
#include <fastdds/rtps/transport/shared_mem/SharedMemTransportDescriptor.hpp>
#include <fastdds/rtps/transport/UDPv4TransportDescriptor.hpp>
#include <fastdds/rtps/transport/TCPv4TransportDescriptor.hpp>
#include <fastdds/rtps/common/Locator.hpp>

#include <compute.hpp>
#include <computePubSubTypes.hpp>


namespace wdndds_internal {

struct wdndds_participant_s
{
    eprosima::fastdds::dds::DomainParticipant* impl = nullptr;
    eprosima::fastdds::dds::Publisher*          publisher = nullptr;
    eprosima::fastdds::dds::Subscriber*         subscriber = nullptr;
    /* Owned Service handles per (service_name). Topics are auto-created
     * and owned by the Service, so the participant only caches the
     * Service* for find/delete. chunk_topics stays because the chunk
     * stream reuses the ComputeRequest Pub-Sub type outside rrpc. */
    std::unordered_map<std::string, eprosima::fastdds::dds::rpc::Service*> services;
    std::unordered_map<std::string, eprosima::fastdds::dds::Topic*> chunk_topics;
    /* Each service_type_name registered exactly once per participant.
     * First register_service_type wins; subsequent services that share
     * the same type name reuse it. */
    std::unordered_map<std::string, eprosima::fastdds::dds::rpc::ServiceTypeSupport> service_types;
};

struct wdndds_replier_s
{
    wdndds_participant_t*    part = nullptr;
    std::string              service_name;
    eprosima::fastdds::dds::rpc::Service* service = nullptr;
    eprosima::fastdds::dds::rpc::Replier* replier = nullptr;
    wdndds_request_cb_t      cb = nullptr;
    wdndds_peer_change_cb_t  peer_cb = nullptr;
    void*                    user = nullptr;
    std::atomic<bool>        stop{false};
    std::thread              pump;
};

struct wdndds_requester_s
{
    wdndds_participant_t*    part = nullptr;
    std::string              service_name;
    eprosima::fastdds::dds::rpc::Service* service = nullptr;
    eprosima::fastdds::dds::rpc::Requester* requester = nullptr;
    std::mutex               mutex;
    /* Track how many rep_reader endpoints are currently matched.
     * Populated by a DataReaderListener attached to the underlying
     * reply reader (``rpc::Requester::get_requester_reader()``); used
     * by ``wdndds_requester_call`` to short-circuit the first-call
     * discovery race — when the requester is brand new the SEDP
     * endpoint announcement hasn't propagated yet, so ``take_reply``
     * would block until the absolute timeout. A short grace-period
     * wait collapses the race into a sub-second delay; if matching
     * really doesn't happen we fail fast with "discovery_pending" so
     * the caller can recreate + retry. */
    std::atomic<int>          rep_reader_matched_count{0};
    /* Keep the listener shared_ptr alive for the lifetime of the
     * requester — otherwise FastDDS holds a dangling raw pointer to
     * the freed listener and segfaults on the first matching event. */
    std::shared_ptr<eprosima::fastdds::dds::DataReaderListener> rep_reader_listener;
};

struct wdndds_topic_writer_s
{
    wdndds_participant_t*                       part = nullptr;
    std::string                                  topic_name;
    eprosima::fastdds::dds::Topic*              topic = nullptr;
    eprosima::fastdds::dds::DataWriter*         writer = nullptr;
    std::shared_ptr<void>                       writer_listener;
};

struct wdndds_topic_reader_s
{
    wdndds_participant_t*                       part = nullptr;
    std::string                                  topic_name;
    eprosima::fastdds::dds::Topic*              topic = nullptr;
    eprosima::fastdds::dds::DataReader*         reader = nullptr;
    std::shared_ptr<void>                       reader_listener;
    void                                      (*cb)(const char*, uint32_t, void*) = nullptr;
    void*                                       user = nullptr;
    std::atomic<bool>                           stop{false};
    std::thread                                 pump;
};

char *copy_str(const std::string &s)
{
    char *p = (char *)std::malloc(s.size() + 1);
    if (!p) return nullptr;
    std::memcpy(p, s.data(), s.size());
    p[s.size()] = '\0';
    return p;
}

char *copy_str(const char *s)
{
    if (!s) return nullptr;
    size_t n = std::strlen(s);
    char *p = (char *)std::malloc(n + 1);
    if (!p) return nullptr;
    std::memcpy(p, s, n + 1);
    return p;
}

std::string req_topic_name(const std::string& svc) {
    /* Kept for diagnostic prints only — FastDDS rrpc actually uses
    // ``<service_name>_Request`` / ``<service_name>_Reply``. */
    return svc + "_Request";
}
std::string rep_topic_name(const std::string& svc) {
    return svc + "_Reply";
}

}  /* namespace wdndds_internal */

class _WriterMatchListener : public eprosima::fastdds::dds::DataWriterListener
{
public:
    _WriterMatchListener(const std::string& svc) : svc_(svc) {}
    void on_publication_matched(
            eprosima::fastdds::dds::DataWriter*,
            const eprosima::fastdds::dds::PublicationMatchedStatus& info) override
    {
        // fprintf(stderr, "[wdndds_cshim] %s writer matched (+%d, -%d) total=%d\n",
        //         svc_.c_str(), info.current_count_change, info.current_count_change,
        //         info.current_count);
    }
private:
    std::string svc_;
};
class _ReaderMatchListener : public eprosima::fastdds::dds::DataReaderListener
{
public:
    /* Optional counter: if non-null, FastDDS-matched publisher count for this
     * reader is mirrored here atomically so callers (``wdndds_requester_call``)
     * can probe whether endpoint discovery has completed without doing a
     * blocking wait themselves. */
    _ReaderMatchListener(const std::string& svc,
                         std::atomic<int>* counter = nullptr)
        : svc_(svc), counter_(counter) {}
    void on_subscription_matched(
            eprosima::fastdds::dds::DataReader*,
            const eprosima::fastdds::dds::SubscriptionMatchedStatus& info) override
    {
        if (counter_) counter_->store(info.current_count);
        // fprintf(stderr, "[wdndds_cshim] %s reader matched (+%d, -%d) total=%d\n",
        //         svc_.c_str(), info.current_count_change, info.current_count_change,
        //         info.current_count);
    }
private:
    std::string svc_;
    std::atomic<int>* counter_;
};
extern "C" {

/* Diagnostics -------------------------------------------------------- */

int32_t wdndds_check_runtime(char **out_err)
{
    try
    {
        auto factory = eprosima::fastdds::dds::DomainParticipantFactory::get_shared_instance();
        if (!factory) {
            if (out_err) *out_err = wdndds_internal::copy_str("DomainParticipantFactory null");
            return -1;
        }
    }
    catch (const std::exception &e)
    {
        if (out_err) *out_err = wdndds_internal::copy_str(e.what());
        return -1;
    }
    return 0;
}

void wdndds_free(char *p)
{
    if (p) std::free(p);
}

/* Parse "host:port" into a UDPv4 Locator. */
static bool parse_locator(const std::string& hp, eprosima::fastdds::rtps::Locator& out)
{
    auto colon = hp.rfind(':');
    if (colon == std::string::npos) return false;
    std::string host = hp.substr(0, colon);
    std::string port_s = hp.substr(colon + 1);
    if (host.empty() || port_s.empty()) return false;
    int port = std::atoi(port_s.c_str());
    if (port <= 0 || port > 65535) return false;

    eprosima::fastdds::rtps::IPLocator::setIPv4(out, host);
    eprosima::fastdds::rtps::IPLocator::setPhysicalPort(out, static_cast<uint16_t>(port));
    return true;
}

/* Participant --------------------------------------------------------- */

wdndds_participant_t *wdndds_participant_create(
    int32_t domain_id,
    const char *participant_name,
    const char *discovery_servers_csv,
    int32_t is_discovery_server)
{
    try
    {
        auto factory = eprosima::fastdds::dds::DomainParticipantFactory::get_shared_instance();
        if (!factory) return nullptr;

        eprosima::fastdds::dds::DomainParticipantQos qos;
        qos.name() = participant_name ? participant_name : "wdnwebui";

        /* Transport selection:
         *   - Daemon (is_discovery_server=true): UDPv4 only. The daemon
         *     doesn't carry data, only discovery metadata, so SHM
         *     would be unused overhead. One descriptor matches the
         *     official FastDDS ServerApp pattern.
        /* Transport selection:
         *   - Daemon (is_discovery_server=true): UDPv4 only. The daemon
         *     doesn't carry data, only discovery metadata, so SHM
         *     would be unused overhead. One descriptor matches the
         *     official FastDDS ServerApp pattern.
         *   - Client: use FastDDS built-in transports (SHM + UDPv4 +
         *     TCPv4). The built-in configuration is well-tested for
         *     small/medium messages; the manual SHM+UDPv4 setup we
         *     used previously wedges the DDS layer's ``write()`` call
         *     on payloads above ~500 KiB (the embed_video frame
         *     buffer wedges the whole RPC). Letting FastDDS pick its
         *     own transports gives us the TCPv4 fallback path for
         *     large messages, which the manual config lacked. */
        if (is_discovery_server)
        {
            qos.transport().use_builtin_transports = false;
            auto udpv4 = std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>();
            qos.transport().user_transports.push_back(udpv4);
        }
        else
        {
            qos.transport().use_builtin_transports = true;
        }

        /* Discovery protocol selection. With discovery_servers_csv:
         *   SERVER protocol for the daemon (listens + brokers).
         *   CLIENT protocol for everyone else (only connects).
         * Without discovery_servers_csv: leave default (SIMPLE). */
        std::vector<std::string> discovery_lines;
        if (discovery_servers_csv && *discovery_servers_csv)
        {
            std::string csv(discovery_servers_csv);
            size_t start = 0;
            while (start <= csv.size())
            {
                size_t end = csv.find('\n', start);
                if (end == std::string::npos) end = csv.size();
                std::string line = csv.substr(start, end - start);
                if (!line.empty()) discovery_lines.push_back(line);
                if (end == csv.size()) break;
                start = end + 1;
            }

            if (!discovery_lines.empty())
            {
                qos.wire_protocol().builtin.discovery_config.discoveryProtocol =
                    is_discovery_server
                        ? eprosima::fastdds::rtps::DiscoveryProtocol::SERVER
                        : eprosima::fastdds::rtps::DiscoveryProtocol::CLIENT;
                qos.wire_protocol().builtin.avoid_builtin_multicast = true;
                qos.wire_protocol().builtin.discovery_config.m_DiscoveryServers.clear();
                qos.wire_protocol().builtin.metatrafficUnicastLocatorList.clear();
                for (const auto& hp : discovery_lines)
                {
                    eprosima::fastdds::rtps::Locator loc;
                    if (parse_locator(hp, loc))
                    {
                        qos.wire_protocol().builtin.discovery_config.m_DiscoveryServers.push_back(loc);
                        if (is_discovery_server &&
                            qos.wire_protocol().builtin.metatrafficUnicastLocatorList.empty())
                        {
                            qos.wire_protocol().builtin.metatrafficUnicastLocatorList.push_back(loc);
                        }
                    }
                    else
                    {
                        fprintf(stderr,
                                "[wdndds_cshim] bad discovery server entry: %s\n",
                                hp.c_str());
                    }
                }
                qos.wire_protocol().builtin.discovery_config.leaseDuration =
                    eprosima::fastdds::dds::Duration_t(20, 0);
                qos.wire_protocol().builtin.discovery_config.leaseDuration_announcementperiod =
                    eprosima::fastdds::dds::Duration_t(2, 0);
            }
        }

        auto *p = factory->create_participant(domain_id, qos);
        if (!p) return nullptr;

        auto *internal = new wdndds_internal::wdndds_participant_s();
        internal->impl = p;
        internal->publisher = p->create_publisher(
            eprosima::fastdds::dds::PUBLISHER_QOS_DEFAULT);
        internal->subscriber = p->create_subscriber(
            eprosima::fastdds::dds::SUBSCRIBER_QOS_DEFAULT);

        if (p->enable() != eprosima::fastdds::dds::RETCODE_OK)
        {
            fprintf(stderr, "[wdndds_cshim] participant enable() failed\n");
            factory->delete_participant(p);
            delete internal;
            return nullptr;
        }

        return reinterpret_cast<wdndds_participant_t *>(internal);
    }
    catch (const std::exception &e)
    {
        fprintf(stderr, "[wdndds_cshim] participant_create exception: %s\n", e.what());
        return nullptr;
    }
}

void wdndds_participant_destroy(wdndds_participant_t *p)
{
    if (!p) return;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    if (internal->impl)
    {
        /* Drop owned Services first — they hold the rrpc topics. */
        for (auto& kv : internal->services) {
            if (kv.second) internal->impl->delete_service(kv.second);
        }
        internal->services.clear();
        if (internal->subscriber) {
            internal->impl->delete_subscriber(internal->subscriber);
            internal->subscriber = nullptr;
        }
        if (internal->publisher) {
            internal->impl->delete_publisher(internal->publisher);
            internal->publisher = nullptr;
        }
        for (auto& kv : internal->chunk_topics) {
            internal->impl->delete_topic(kv.second);
        }
        auto factory = eprosima::fastdds::dds::DomainParticipantFactory::get_shared_instance();
        factory->delete_participant(internal->impl);
        internal->impl = nullptr;
    }
    delete internal;
}

int32_t wdndds_participant_guid(
    wdndds_participant_t *p,
    char                 *out_guid,
    uint32_t              out_guid_len)
{
    if (!p || !out_guid || out_guid_len < 33) return -1;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    try
    {
        auto guid = internal->impl->guid();
        char* p_out = out_guid;
        const uint8_t* bytes = guid.guidPrefix.value;
        for (int i = 0; i < 12; ++i)
        {
            std::snprintf(p_out, 4, "%02X.", bytes[i]);
            p_out += 3;
        }
        for (int i = 0; i < 3; ++i)
        {
            std::snprintf(p_out, 4, "%02X.", guid.entityId.value[i]);
            p_out += 3;
        }
        std::snprintf(p_out, 3, "%02X", guid.entityId.value[3]);
        p_out += 2;
        *p_out = '\0';
        return 0;
    }
    catch (const std::exception& e)
    {
        fprintf(stderr, "[wdndds_cshim] guid() exception: %s\n", e.what());
        return -1;
    }
}

int32_t wdndds_participant_list_peers(
    wdndds_participant_t *p,
    char                **out_peers,
    uint32_t             *out_len)
{
    (void)p;
    if (!out_peers || !out_len) return -1;
    *out_peers = wdndds_internal::copy_str(std::string(""));
    *out_len = 0;
    return 0;
}

/* Service (real rrpc) ------------------------------------------------- */

/* Service type name — single shared per participant. */
static constexpr const char* kComputeServiceTypeName = "wdndds::rpc::ComputeService";

/* Register the (req_type, rep_type) pair on the participant exactly
 * once, returning a stable reference into the participant's
 * service_types cache. Subsequent services that share the type name
 * reuse the existing registration (FastDDS rejects a second
 * register_service_type with the same name). */
static eprosima::fastdds::dds::rpc::ServiceTypeSupport&
get_or_register_compute_service_type(
    wdndds_internal::wdndds_participant_s* pin)
{
    auto it = pin->service_types.find(kComputeServiceTypeName);
    if (it != pin->service_types.end()) return it->second;

    eprosima::fastdds::dds::TypeSupport req_type(
        new ComputeRequestPubSubType());
    eprosima::fastdds::dds::TypeSupport rep_type(
        new ComputeReplyPubSubType());
    /* Bumped to 1 MiB (1048576) to also fit raw-bytes chunks carried
     * via the same ComputeRequest envelope (see wdndds_topic_writer/
     * reader below). First register_type() wins for the
     * "ComputeRequest" / "ComputeReply" type names; both RPC and
     * chunk paths share this single cap. */
    req_type->max_serialized_type_size = 1048576;
    rep_type->max_serialized_type_size = 1048576;
    /* Pre-register types on the participant so ServiceTypeSupport
     * registration succeeds when the type name is shared. */
    req_type.register_type(pin->impl, "ComputeRequest");
    rep_type.register_type(pin->impl, "ComputeReply");

    auto inserted = pin->service_types.emplace(
        std::string(kComputeServiceTypeName),
        eprosima::fastdds::dds::rpc::ServiceTypeSupport(
            std::move(req_type), std::move(rep_type)));
    return inserted.first->second;
}

wdndds_service_t *wdndds_service_create(
    wdndds_participant_t *p,
    const char *service_name)
{
    if (!p || !service_name) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);

    try
    {
        std::string svc(service_name);

        /* Register / fetch the shared ServiceTypeSupport. */
        auto& svc_type = get_or_register_compute_service_type(pin);
        eprosima::fastdds::dds::ReturnCode_t reg_rc =
            svc_type.register_service_type(pin->impl, kComputeServiceTypeName);
        if (reg_rc != eprosima::fastdds::dds::RETCODE_OK)
        {
            /* RETCODE_PRECONDITION_NOT_MET = "another service with the
             * same type name already registered" → fine, the existing
             * registration wins and we still create the Service. */
            if (reg_rc != eprosima::fastdds::dds::RETCODE_PRECONDITION_NOT_MET)
            {
                fprintf(stderr, "[wdndds_cshim] register_service_type failed for %s rc=%d\n",
                        svc.c_str(), (int)reg_rc);
                return nullptr;
            }
        }

        /* create_service auto-creates <service_name>_Request +
         * <service_name>_Reply topics and registers them on the
         * participant; topics are owned by the Service and disposed
         * with it. */
        auto *service = pin->impl->create_service(svc, kComputeServiceTypeName);
        if (!service)
        {
            fprintf(stderr, "[wdndds_cshim] create_service failed for %s\n", svc.c_str());
            return nullptr;
        }
        pin->services[svc] = service;

        auto *s = new wdndds_internal::wdndds_replier_s();
        s->part = p;
        s->service_name = svc;
        s->service = service;
        return reinterpret_cast<wdndds_service_t *>(s);
    }
    catch (const std::exception &e)
    {
        fprintf(stderr, "[wdndds_cshim] service_create exception: %s\n", e.what());
        return nullptr;
    }
}

void wdndds_service_destroy(wdndds_service_t *s)
{
    if (!s) return;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_replier_s *>(s);
    if (internal->service && internal->part)
    {
        auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(internal->part);
        pin->impl->delete_service(internal->service);
        auto it = pin->services.find(internal->service_name);
        if (it != pin->services.end()) pin->services.erase(it);
    }
    delete internal;
}

/* Replier (server) -------------------------------------------------- */

wdndds_replier_t *wdndds_replier_create(
    wdndds_participant_t *p,
    wdndds_service_t *s,
    wdndds_request_cb_t cb,
    wdndds_peer_change_cb_t peer_cb,
    void *user)
{
    if (!p || !s || !cb) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    auto *sin = reinterpret_cast<wdndds_internal::wdndds_replier_s *>(s);

    try
    {
        std::string svc = sin->service_name;

        eprosima::fastdds::dds::ReplierQos replier_qos;
        /* ReplierQos defaults: RELIABLE, VOLATILE, KEEP_ALL — matches
         * our previous manual DataReader/DataWriter QoS. No customisation
         * required. */
        (void)replier_qos;

        eprosima::fastdds::dds::ReturnCode_t rc = eprosima::fastdds::dds::RETCODE_OK;
        auto *replier = pin->impl->create_service_replier(sin->service, replier_qos, rc);
        if (!replier || rc != eprosima::fastdds::dds::RETCODE_OK)
        {
            fprintf(stderr, "[wdndds_cshim] create_service_replier failed for %s rc=%d\n",
                    svc.c_str(), (int)rc);
            return nullptr;
        }

        auto *r = new wdndds_internal::wdndds_replier_s();
        r->part = p;
        r->service_name = svc;
        r->service = sin->service;
        r->replier = replier;
        r->cb = cb;
        r->peer_cb = peer_cb;
        r->user = user;

        r->pump = std::thread([r]() {
            while (!r->stop.load())
            {
                ComputeRequest req;
                eprosima::fastdds::dds::rpc::RequestInfo info;
                auto rc_take = r->replier->take_request(&req, info);
                if (rc_take == eprosima::fastdds::dds::RETCODE_OK)
                {
                    std::string action_str;
                    switch (req.action())
                    {
                        case WDNDDS_REQUEST_ACTION_INFO:     action_str = "info"; break;
                        case WDNDDS_REQUEST_ACTION_LOAD:     action_str = "load"; break;
                        case WDNDDS_REQUEST_ACTION_INFER:    action_str = "infer"; break;
                        case WDNDDS_REQUEST_ACTION_RESET:    action_str = "reset"; break;
                        case WDNDDS_REQUEST_ACTION_SHUTDOWN: action_str = "shutdown"; break;
                        default: action_str = "info"; break;
                    }

                    /* The client cshim stuffs the entire original JSON
                     * into ``req.prompt()`` (because the typed IDL only
                     * carries action / every_n / request_id + a prompt
                     * string). Re-quote it as a JSON string value with
                     * proper escaping so the user's Python callback can
                     * json.loads the rebuilt payload without choking on
                     * embedded quotes / backslashes from base64 image
                     * data. */
                    std::string esc_prompt;
                    esc_prompt.reserve(req.prompt().size() + 16);
                    for (char c : req.prompt())
                    {
                        switch (c) {
                            case '"':  esc_prompt += "\\\""; break;
                            case '\\': esc_prompt += "\\\\"; break;
                            case '\n': esc_prompt += "\\n";  break;
                            case '\r': esc_prompt += "\\r";  break;
                            case '\t': esc_prompt += "\\t";  break;
                            default:
                                if (static_cast<unsigned char>(c) < 0x20)
                                    esc_prompt += ' ';
                                else
                                    esc_prompt += c;
                        }
                    }

                    std::string payload_json =
                        std::string("{\"action\":\"") + action_str + "\"," +
                        std::string("\"prompt\":\"") + esc_prompt + "\"," +
                        std::string("\"every_n\":") + std::to_string(req.every_n()) + "," +
                        std::string("\"request_id\":") + std::to_string(req.request_id()) +
                        "}";

                    /* correlation_id slot — opaque int64 — pack the
                     * writer sequence_number so the user callback sees a
                     * stable, non-zero identifier. The high 32 bits of
                     * the full int64_t sequence_number are dropped
                     * (sufficient for per-replier traffic; the Python
                     * wrapper currently ignores this field entirely). */
                    int64_t correlation_id =
                        static_cast<int64_t>(info.sample_identity.sequence_number().low);

                    char *out_reply = nullptr;
                    uint32_t out_len = 0;
                    int32_t cb_rc = r->cb(
                        r->service_name.c_str(),
                        payload_json.data(),
                        static_cast<uint32_t>(payload_json.size()),
                        correlation_id,
                        r->user,
                        &out_reply, &out_len);

                    ComputeReply reply;
                    reply.request_id(req.request_id());
                    if (cb_rc == 0 && out_reply && out_len > 0)
                    {
                        reply.text() = std::string(out_reply, out_len);
                        reply.ok(true);
                    }
                    else
                    {
                        reply.ok(false);
                        reply.error(out_reply ? std::string(out_reply, out_len) : "callback failed");
                    }
                    /* Do NOT free(out_reply) — when the cb is a Python
                     * CFUNCTYPE, out_reply is a ctypes-managed c_char_p
                     * whose lifetime is bound to the Python frame.
                     * Freeing it here with std::free() corrupts the
                     * heap ("munmap_chunk(): invalid pointer").
                     * The cshim contract is: copy the bytes into the
                     * reply struct, then ignore out_reply. The Python
                     * side releases its ctypes buffer when the call
                     * returns. */
                    (void)0;

                    /* Standard DDS-RPC routing: pass the RequestInfo
                     * straight through so FastDDS stamps the reply with
                     * the matching SampleIdentity (writer GUID +
                     * sequence_number) and the originating requester
                     * correlates by it. */
                    auto rc_send = r->replier->send_reply(&reply, info);
                    if (rc_send == eprosima::fastdds::dds::RETCODE_NO_DATA)
                    {
                        /* The originating requester disconnected
                         * between take_request and send_reply — drop
                         * the reply silently, the spec allows it. */
                    }
                }
                else if (rc_take == eprosima::fastdds::dds::RETCODE_NO_DATA)
                {
                    /* Spec-compliant spin-wait with short backoff; on
                     * real builds take_request also takes a
                     * Duration_t max_wait overload, but the no-arg
                     * overload returns NO_DATA when nothing is queued,
                     * which is the expected steady-state outcome. */
                    std::this_thread::sleep_for(std::chrono::milliseconds(2));
                }
                else
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                }
            }
        });

        return reinterpret_cast<wdndds_replier_t *>(r);
    }
    catch (const std::exception &e)
    {
        fprintf(stderr, "[wdndds_cshim] replier_create exception: %s\n", e.what());
        return nullptr;
    }
}

void wdndds_replier_destroy(wdndds_replier_t *r)
{
    if (!r) return;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_replier_s *>(r);
    internal->stop.store(true);
    if (internal->pump.joinable()) internal->pump.join();
    if (internal->replier && internal->part)
    {
        auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(internal->part);
        pin->impl->delete_service_replier(internal->service_name, internal->replier);
    }
    delete internal;
}

/* Requester (client) ----------------------------------------------- */

wdndds_requester_t *wdndds_requester_create(
    wdndds_participant_t *p,
    wdndds_service_t *s,
    int32_t timeout_ms)
{
    (void)timeout_ms;
    if (!p || !s) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    auto *sin = reinterpret_cast<wdndds_internal::wdndds_replier_s *>(s);

    try
    {
        std::string svc = sin->service_name;

        eprosima::fastdds::dds::RequesterQos requester_qos;
        /* RequesterQos defaults: RELIABLE, VOLATILE, KEEP_ALL — matches
         * the previous manual plumbing. */
        (void)requester_qos;

        eprosima::fastdds::dds::ReturnCode_t rc = eprosima::fastdds::dds::RETCODE_OK;
        auto *requester = pin->impl->create_service_requester(
            sin->service, requester_qos, rc);
        if (!requester || rc != eprosima::fastdds::dds::RETCODE_OK)
        {
            fprintf(stderr, "[wdndds_cshim] create_service_requester failed for %s rc=%d\n",
                    svc.c_str(), (int)rc);
            return nullptr;
        }

        auto *r = new wdndds_internal::wdndds_requester_s();
        r->part = p;
        r->service_name = svc;
        r->service = sin->service;
        r->requester = requester;

        /* Attach a DataReaderListener to the underlying reply reader
         * so we can mirror its matched-publisher count. FastDDS holds
         * a raw pointer to the listener; keeping the shared_ptr alive
         * for the requester's lifetime avoids a use-after-free on
         * matching events (segfault seen in v0.3 when the listener
         * went out of scope here). */
        auto listener = std::make_shared<_ReaderMatchListener>(
            svc + "/rep", &r->rep_reader_matched_count);
        auto *raw_reader = requester->get_requester_reader();
        if (raw_reader)
        {
            raw_reader->set_listener(listener.get());
        }
        r->rep_reader_listener = std::move(listener);

        return reinterpret_cast<wdndds_requester_t *>(r);
    }
    catch (const std::exception &e)
    {
        fprintf(stderr, "[wdndds_cshim] requester_create exception: %s\n", e.what());
        return nullptr;
    }
}

void wdndds_requester_destroy(wdndds_requester_t *r)
{
    if (!r) return;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_requester_s *>(r);
    /* Reset the listener before deleting the requester so FastDDS
     * stops calling back into us while the atomic counter is being
     * torn down. */
    if (internal->requester)
    {
        auto *raw_reader = internal->requester->get_requester_reader();
        if (raw_reader) raw_reader->set_listener(nullptr);
    }
    internal->rep_reader_listener.reset();
    if (internal->requester && internal->part)
    {
        auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(internal->part);
        pin->impl->delete_service_requester(internal->service_name, internal->requester);
    }
    delete internal;
}

int32_t wdndds_requester_call(
    wdndds_requester_t *r,
    const char *payload,
    uint32_t payload_len,
    int32_t timeout_ms,
    char **out_reply,
    uint32_t *out_reply_len)
{
    if (!r || !payload || !out_reply || !out_reply_len) return -1;
    auto *internal = reinterpret_cast<wdndds_internal::wdndds_requester_s *>(r);

    std::lock_guard<std::mutex> lock(internal->mutex);

    try
    {
        std::string json(payload, payload_len);
        std::string action = "info", prompt = "";
        int64_t every_n = 0, request_id = 0;

        auto find_str = [&](const std::string &key, std::string &out) {
            auto k = std::string("\"") + key + "\":";
            auto p = json.find(k);
            if (p == std::string::npos) return false;
            p += k.size();
            while (p < json.size() && (json[p] == ' ' || json[p] == '\t')) ++p;
            if (p >= json.size() || json[p] != '"') return false;
            ++p;
            std::string buf;
            while (p < json.size() && json[p] != '"')
            {
                if (json[p] == '\\' && p + 1 < json.size())
                {
                    char next = json[p + 1];
                    switch (next) {
                        case 'n': buf += '\n'; break;
                        case 'r': buf += '\r'; break;
                        case 't': buf += '\t'; break;
                        case '"': buf += '"'; break;
                        case '\\': buf += '\\'; break;
                        default: buf += next;
                    }
                    p += 2;
                }
                else
                {
                    buf += json[p++];
                }
            }
            out = buf;
            return true;
        };

        auto find_int = [&](const std::string &key, int64_t &out) {
            auto k = std::string("\"") + key + "\":";
            auto p = json.find(k);
            if (p == std::string::npos) return false;
            p += k.size();
            while (p < json.size() && (json[p] == ' ' || json[p] == '\t')) ++p;
            std::string buf;
            while (p < json.size() && (json[p] == '-' ||
                   (json[p] >= '0' && json[p] <= '9')))
            {
                buf += json[p++];
            }
            if (buf.empty()) return false;
            out = std::atoll(buf.c_str());
            return true;
        };

find_str("action", action);
        find_int("every_n", every_n);
        find_int("request_id", request_id);
        if (request_id == 0)
        {
            using namespace std::chrono;
            request_id = duration_cast<nanoseconds>(
                steady_clock::now().time_since_epoch()).count();
        }

        int32_t action_id = WDNDDS_REQUEST_ACTION_INFO;
        if (action == "info") action_id = WDNDDS_REQUEST_ACTION_INFO;
        else if (action == "load") action_id = WDNDDS_REQUEST_ACTION_LOAD;
        else if (action == "infer") action_id = WDNDDS_REQUEST_ACTION_INFER;
        else if (action == "reset") action_id = WDNDDS_REQUEST_ACTION_RESET;
        else if (action == "shutdown") action_id = WDNDDS_REQUEST_ACTION_SHUTDOWN;

        /* Pass the entire client JSON through the typed ``prompt``
         * slot. The IDL only carries action / every_n / request_id
         * + a ``prompt`` string; everything else (``attachment``,
         * ``generating``, ``tools``, ``options``, ...) would be
         * silently dropped. IDL strings are unbounded in FastDDS so
         * the whole JSON (including base64 image data) fits.
         *
         * The worker cshim rebuilds payload_json by re-quoting
         * ``req.prompt()`` with proper JSON-escape; the worker's
         * router then detects a JSON-looking prompt and re-parses
         * it to recover the original payload dict. */
        prompt = json;

        ComputeRequest req;
        req.request_id(request_id);
        req.action(action_id);
        req.every_n(every_n);
        req.prompt(prompt);

        /* Real DDS-RPC send. ``RequestInfo`` is populated by
         * ``send_request`` with our outgoing write's SampleIdentity
         * (writer GUID + sequence_number); we keep it to match the
         * reply later by ``related_sample_identity``. */
        eprosima::fastdds::dds::rpc::RequestInfo sent_info;
        auto rc = internal->requester->send_request(&req, sent_info);
        if (rc != eprosima::fastdds::dds::RETCODE_OK)
        {
            std::string err = "{\"ok\":false,\"error\":\"send_request rc=" +
                std::to_string((int)rc) + "\"}";
            *out_reply = wdndds_internal::copy_str(err);
            *out_reply_len = (uint32_t)err.size();
            return -1;
        }

        /* Discovery wait: a fresh requester hasn't finished its SEDP
         * handshake yet, so ``take_reply`` would block until the
         * absolute deadline. Wait briefly for the reply reader to
         * discover at least one matching replier writer before
         * starting the polling loop; if matching doesn't happen in
         * the grace period, fail fast with "discovery_pending" so the
         * caller can recreate + retry instead of waiting the full
         * timeout. */
        {
            const int discovery_grace_ms = std::min(timeout_ms > 0 ? timeout_ms : 10000, 5000);
            const auto disc_deadline = std::chrono::steady_clock::now() +
                std::chrono::milliseconds(discovery_grace_ms);
            while (std::chrono::steady_clock::now() < disc_deadline)
            {
                if (internal->rep_reader_matched_count.load() > 0) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (internal->rep_reader_matched_count.load() == 0)
            {
                std::string err = "{\"ok\":false,\"error\":\"discovery_pending\"}";
                *out_reply = wdndds_internal::copy_str(err);
                *out_reply_len = (uint32_t)err.size();
                return 1;
            }
        }

        auto deadline = std::chrono::steady_clock::now() +
            std::chrono::milliseconds(timeout_ms > 0 ? timeout_ms : 10000);

        while (std::chrono::steady_clock::now() < deadline)
        {
            ComputeReply reply;
            eprosima::fastdds::dds::rpc::RequestInfo reply_info;
            auto rc2 = internal->requester->take_reply(&reply, reply_info);
            if (rc2 == eprosima::fastdds::dds::RETCODE_OK)
            {
                /* Standard DDS-RPC correlation: match the reply's
                 * ``related_sample_identity`` (the request's
                 * SampleIdentity that the Replier stamped on
                 * ``send_reply``) against our outgoing request. */
                const auto& sent = sent_info.sample_identity;
                const auto& got = reply_info.related_sample_identity;
                bool match = (sent.writer_guid() == got.writer_guid() &&
                              sent.sequence_number() == got.sequence_number());
                if (!match) continue;

                /* The user callback returns a Python dict which
                 * ``Replier._on_request_c`` json.dumps to ``reply.text()``
                 * — i.e. ``reply.text()`` is already a valid JSON object
                 * string. Embed it as a raw JSON object under ``data``
                 * so consumers can access fields directly
                 * (``r["data"]["status"]``, etc.) without an extra
                 * parse step. */
                std::string json_reply = "{";
                json_reply += std::string("\"ok\":") + (reply.ok() ? "true" : "false");
                json_reply += ",\"request_id\":" + std::to_string(reply.request_id());
                if (!reply.text().empty())
                    json_reply += ",\"data\":" + reply.text();
                if (!reply.error().empty())
                {
                    /* Escape control chars + quotes for embedding as
                     * a JSON string value. */
                    std::string esc;
                    esc.reserve(reply.error().size() + 8);
                    for (char c : reply.error())
                    {
                        switch (c) {
                            case '"':  esc += "\\\""; break;
                            case '\\': esc += "\\\\"; break;
                            case '\n': esc += "\\n";  break;
                            case '\r': esc += "\\r";  break;
                            case '\t': esc += "\\t";  break;
                            default:
                                if (static_cast<unsigned char>(c) < 0x20)
                                    esc += ' ';
                                else
                                    esc += c;
                        }
                    }
                    json_reply += ",\"error\":\"" + esc + "\"";
                }
                json_reply += "}";
                *out_reply = wdndds_internal::copy_str(json_reply);
                *out_reply_len = (uint32_t)json_reply.size();
                return 0;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }

        std::string err = "{\"ok\":false,\"error\":\"timeout\"}";
        *out_reply = wdndds_internal::copy_str(err);
        *out_reply_len = (uint32_t)err.size();
        return 1;
    }
    catch (const std::exception &e)
    {
        std::string err = std::string("{\"ok\":false,\"error\":\"") + e.what() + "\"}";
        *out_reply = wdndds_internal::copy_str(err);
        *out_reply_len = (uint32_t)err.size();
        return -1;
    }
}

int32_t wdndds_requester_matched_count(wdndds_requester_t *r)
{
    if (!r) return 0;
    return reinterpret_cast<wdndds_internal::wdndds_requester_s *>(r)
        ->rep_reader_matched_count.load();
}

/* Topic (raw-bytes stream) ----------------------------------------------- */

namespace wdndds_internal {

/* Get or create a chunk-stream topic registered with the existing
 * "ComputeRequest" TypeSupport. Cached on the participant so multiple
 * writers/readers on the same topic name share one Topic instance
 * (required for endpoint matching). Topic is deleted with the
 * participant — chunk_*_destroy() does NOT delete it. */
static eprosima::fastdds::dds::Topic* get_or_create_chunk_topic(
    wdndds_internal::wdndds_participant_s* pin,
    const std::string& topic_name,
    eprosima::fastdds::dds::TypeSupport& ts)
{
    auto it = pin->chunk_topics.find(topic_name);
    if (it != pin->chunk_topics.end()) return it->second;
    if (!ts.empty())
    {
        ts.register_type(pin->impl, "ComputeRequest");
    }
    auto* topic = pin->impl->create_topic(
        topic_name, "ComputeRequest",
        eprosima::fastdds::dds::TOPIC_QOS_DEFAULT);
    if (topic) pin->chunk_topics[topic_name] = topic;
    return topic;
}

}  /* namespace wdndds_internal */

wdndds_topic_writer_t *wdndds_topic_writer_create(
    wdndds_participant_t *p,
    const char           *topic_name,
    int32_t               qos_kind)
{
    if (!p || !topic_name) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);

    try
    {
        eprosima::fastdds::dds::TypeSupport req_type(
            new ComputeRequestPubSubType());
        req_type->max_serialized_type_size = 1048576;
        std::string tn(topic_name);
        auto* topic = wdndds_internal::get_or_create_chunk_topic(pin, tn, req_type);
        if (!topic) {
            fprintf(stderr, "[wdndds_cshim] topic_writer_create: "
                    "failed to create topic '%s'\n", tn.c_str());
            return nullptr;
        }

        eprosima::fastdds::dds::DataWriterQos wqos;
        if (qos_kind == WDNDDS_TOPIC_QOS_RELIABLE)
        {
            wqos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
            wqos.history().depth = 16;
        }
        else
        {
            wqos.reliability().kind = eprosima::fastdds::dds::BEST_EFFORT_RELIABILITY_QOS;
            wqos.history().depth = 4;
        }
        wqos.durability().kind = eprosima::fastdds::dds::VOLATILE_DURABILITY_QOS;

        auto writer_listener = std::make_shared<_WriterMatchListener>(tn);
        auto* writer = pin->publisher->create_datawriter(
            topic, wqos, writer_listener.get());
        if (!writer) {
            fprintf(stderr, "[wdndds_cshim] topic_writer_create: "
                    "create_datawriter failed for '%s'\n", tn.c_str());
            return nullptr;
        }
        writer->enable();

        auto* w = new wdndds_internal::wdndds_topic_writer_s();
        w->part = p;
        w->topic_name = tn;
        w->topic = topic;
        w->writer = writer;
        w->writer_listener = writer_listener;
        return reinterpret_cast<wdndds_topic_writer_t *>(w);
    }
    catch (const std::exception& e)
    {
        fprintf(stderr, "[wdndds_cshim] topic_writer_create exception: %s\n", e.what());
        return nullptr;
    }
}

int32_t wdndds_topic_writer_write(
    wdndds_topic_writer_t *w,
    const char            *payload,
    uint32_t               payload_len)
{
    if (!w || !payload) return -1;
    auto* win = reinterpret_cast<wdndds_internal::wdndds_topic_writer_s *>(w);
    if (!win->writer) return -1;

    ComputeRequest req;
    /* Stamp request_id with a monotonic counter so duplicate chunks
     * (e.g. retransmits on RELIABLE QoS) don't collide. */
    static std::atomic<uint64_t> counter{0};
    req.request_id(static_cast<int64_t>(counter.fetch_add(1, std::memory_order_relaxed)));
    req.action(WDNDDS_REQUEST_ACTION_INFO);
    req.every_n(0);
    req.prompt("");
    req.model_id("");
    req.npu_index(0);

    ComputeAttachment att;
    att.kind(WDNDDS_ATTACHMENT_KIND_NDARRAY);
    att.mime_type("application/octet-stream");
    att.encoding(WDNDDS_IMAGE_ENCODING_RAW);
    att.width(0); att.height(0); att.channels(0); att.row_stride(0);
    att.bytes().assign(
        reinterpret_cast<const uint8_t*>(payload),
        reinterpret_cast<const uint8_t*>(payload) + payload_len);

    std::vector<ComputeAttachment> atts;
    atts.push_back(att);
    req.attachments(atts);

    auto rc = win->writer->write(&req);
    return rc == eprosima::fastdds::dds::RETCODE_OK ? 0 : (int32_t)rc;
}

void wdndds_topic_writer_destroy(wdndds_topic_writer_t *w)
{
    if (!w) return;
    auto* win = reinterpret_cast<wdndds_internal::wdndds_topic_writer_s *>(w);
    if (win->part)
    {
        auto* pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(win->part);
        if (win->writer && pin->publisher) {
            pin->publisher->delete_datawriter(win->writer);
        }
    }
    /* Topic stays cached on the participant — deleted by
     * wdndds_participant_destroy(). */
    win->writer = nullptr;
    win->topic = nullptr;
    win->writer_listener.reset();
    delete win;
}

wdndds_topic_reader_t *wdndds_topic_reader_create(
    wdndds_participant_t *p,
    const char           *topic_name,
    int32_t               qos_kind,
    void                (*callback)(const char *payload, uint32_t payload_len, void *user),
    void                 *user)
{
    if (!p || !topic_name || !callback) return nullptr;
    auto* pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);

    try
    {
        eprosima::fastdds::dds::TypeSupport req_type(
            new ComputeRequestPubSubType());
        req_type->max_serialized_type_size = 1048576;
        std::string tn(topic_name);
        auto* topic = wdndds_internal::get_or_create_chunk_topic(pin, tn, req_type);
        if (!topic) {
            fprintf(stderr, "[wdndds_cshim] topic_reader_create: "
                    "failed to create topic '%s'\n", tn.c_str());
            return nullptr;
        }

        eprosima::fastdds::dds::DataReaderQos rqos;
        if (qos_kind == WDNDDS_TOPIC_QOS_RELIABLE)
        {
            rqos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
            rqos.history().depth = 16;
        }
        else
        {
            rqos.reliability().kind = eprosima::fastdds::dds::BEST_EFFORT_RELIABILITY_QOS;
            rqos.history().depth = 4;
        }
        rqos.durability().kind = eprosima::fastdds::dds::VOLATILE_DURABILITY_QOS;

        auto reader_listener = std::make_shared<_ReaderMatchListener>(tn + "/reader");
        auto* reader = pin->subscriber->create_datareader(
            topic, rqos, reader_listener.get());
        if (!reader) {
            fprintf(stderr, "[wdndds_cshim] topic_reader_create: "
                    "create_datareader failed for '%s'\n", tn.c_str());
            return nullptr;
        }
        reader->enable();

        auto* r = new wdndds_internal::wdndds_topic_reader_s();
        r->part = p;
        r->topic_name = tn;
        r->topic = topic;
        r->reader = reader;
        r->reader_listener = reader_listener;
        r->cb = callback;
        r->user = user;

        r->pump = std::thread([r]() {
            while (!r->stop.load())
            {
                ComputeRequest req;
                eprosima::fastdds::dds::SampleInfo info;
                auto rc = r->reader->take_next_sample(&req, &info);
                if (rc == eprosima::fastdds::dds::RETCODE_OK && info.valid_data)
                {
                    const auto& atts = req.attachments();
                    if (!atts.empty())
                    {
                        const auto& bytes = atts[0].bytes();
                        r->cb(
                            reinterpret_cast<const char*>(bytes.data()),
                            static_cast<uint32_t>(bytes.size()),
                            r->user);
                    }
                }
                else
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(2));
                }
            }
        });

        return reinterpret_cast<wdndds_topic_reader_t *>(r);
    }
    catch (const std::exception& e)
    {
        fprintf(stderr, "[wdndds_cshim] topic_reader_create exception: %s\n", e.what());
        return nullptr;
    }
}

void wdndds_topic_reader_destroy(wdndds_topic_reader_t *r)
{
    if (!r) return;
    auto* rin = reinterpret_cast<wdndds_internal::wdndds_topic_reader_s *>(r);
    rin->stop.store(true);
    if (rin->pump.joinable()) rin->pump.join();
    if (rin->part)
    {
        auto* pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(rin->part);
        if (rin->reader && pin->subscriber) {
            pin->subscriber->delete_datareader(rin->reader);
        }
    }
    rin->reader = nullptr;
    rin->topic = nullptr;
    rin->reader_listener.reset();
    delete rin;
}

}  /* extern "C" */

/* DomainParticipantListener that fires wdndds_peer_event_cb_t */
class _PeerEventListener : public eprosima::fastdds::dds::DomainParticipantListener
{
public:
    _PeerEventListener(wdndds_peer_event_cb_t cb, void* user) : cb_(cb), user_(user) {}

    void on_participant_discovery(
            eprosima::fastdds::dds::DomainParticipant*,
            eprosima::fastdds::rtps::ParticipantDiscoveryStatus status,
            const eprosima::fastdds::dds::ParticipantBuiltinTopicData& info,
            bool& /*should_be_ignored*/) override
    {
        if (!cb_) return;
        char guid_hex[64];
        char* p_out = guid_hex;
        const uint8_t* bytes = info.guid.guidPrefix.value;
        for (int i = 0; i < 12; ++i) {
            std::snprintf(p_out, 4, "%02X.", bytes[i]);
            p_out += 3;
        }
        for (int i = 0; i < 3; ++i) {
            std::snprintf(p_out, 4, "%02X.", info.guid.entityId.value[i]);
            p_out += 3;
        }
        std::snprintf(p_out, 3, "%02X", info.guid.entityId.value[3]);
        guid_hex[47] = '\0';

        int32_t discovered = 0;
        if (status == eprosima::fastdds::rtps::ParticipantDiscoveryStatus::DISCOVERED_PARTICIPANT)
            discovered = 1;

        // First UDPv4 unicast IP from default_locators; fall back to
        // metatraffic_locators. IPv4 sits in bytes 12..15 of Locator_t.address
        // (16-byte field shared with IPv6).
        char ip_str[16] = {0};
        const auto fill_ip = [&ip_str](const auto& list) {
            for (const auto& loc : list) {
                if (loc.kind == LOCATOR_KIND_UDPv4) {
                    std::snprintf(ip_str, sizeof(ip_str), "%u.%u.%u.%u",
                                  static_cast<unsigned>(loc.address[12]),
                                  static_cast<unsigned>(loc.address[13]),
                                  static_cast<unsigned>(loc.address[14]),
                                  static_cast<unsigned>(loc.address[15]));
                    return true;
                }
            }
            return false;
        };
        if (!fill_ip(info.default_locators.unicast)) {
            fill_ip(info.metatraffic_locators.unicast);
        }

        cb_(guid_hex, info.participant_name.c_str(), ip_str, discovered, user_);
    }

private:
    wdndds_peer_event_cb_t cb_;
    void* user_;
};

int32_t wdndds_participant_set_listener(
    wdndds_participant_t     *p,
    wdndds_peer_event_cb_t    cb,
    void                     *user)
{
    if (!p) return -1;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    auto *listener = new _PeerEventListener(cb, user);
    auto rc = pin->impl->set_listener(listener);
    if (rc != eprosima::fastdds::dds::RETCODE_OK) {
        delete listener;
        return -1;
    }
    return 0;
}

