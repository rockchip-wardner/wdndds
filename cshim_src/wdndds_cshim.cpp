/* wdndds_cshim.cpp — extern "C" wrapper around FastDDS 3.6.
 *
 * Uses raw DataReader/DataWriter + IDL-generated ComputeRequest /
 * ComputeReply types. (FastDDS 3.6's rrpc Service API is declared in
 * the headers but stubbed out in the bodies — return RETCODE_UNSUPPORTED
 * in DomainParticipantImpl.cpp. We work around by using the lower-level
 * primitives directly.)
 *
 * Wire format: Pub-Sub on RELIABLE QoS, two topics per service
 * ("wdndds/rpc/req/<service>" + "wdndds/rpc/rep/<service>"). The
 * payload type is ComputeRequest (req) / ComputeReply (reply) — same
 * IDL, same types, just routed through DataReader/DataWriter instead
 * of Service/Replier/Requester.
 *
 * Transport: SHM (local) + UDPv4 (discovery) + TCPv4 (cross-host).
 *   - Local same-host: data goes via shared memory.
 *   - Cross-host: data via TCPv4 fallback (caller encodes image before send).
 *
 * Discovery:
 *   - Default (no discovery_servers_csv): SIMPLE discovery + multicast
 *     + initialPeersList. Works in-process on the same host.
 *   - With discovery_servers_csv + is_discovery_server=1: SERVER
 *     protocol on the daemon (used by wdndds_daemon).
 *   - With discovery_servers_csv + is_discovery_server=0: CLIENT
 *     protocol pointing to the daemon (used by every pyrkaiweb
 *     process — main / compute_worker / im-bridge).
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
    std::unordered_map<std::string, eprosima::fastdds::dds::Topic*> req_topics;
    std::unordered_map<std::string, eprosima::fastdds::dds::Topic*> rep_topics;
};

struct wdndds_replier_s
{
    wdndds_participant_t*    part = nullptr;
    std::string              service_name;
    eprosima::fastdds::dds::DataReader* req_reader = nullptr;
    eprosima::fastdds::dds::DataWriter* rep_writer = nullptr;
    wdndds_request_cb_t      cb = nullptr;
    wdndds_peer_change_cb_t  peer_cb = nullptr;
    void*                    user = nullptr;
    std::atomic<bool>        stop{false};
    std::thread              pump;
    /* Keep the listener shared_ptrs alive for the lifetime of the
     * replier — otherwise FastDDS holds dangling raw pointers to the
     * freed listeners and segfaults on the first matching event. */
    std::shared_ptr<void> req_reader_listener;
    std::shared_ptr<void> rep_writer_listener;
};

struct wdndds_requester_s
{
    wdndds_participant_t*    part = nullptr;
    std::string              service_name;
    eprosima::fastdds::dds::DataWriter* req_writer = nullptr;
    eprosima::fastdds::dds::DataReader* rep_reader = nullptr;
    std::mutex               mutex;
    /* Keep the listener shared_ptrs alive for the lifetime of the
     * requester — see wdndds_replier_s for the segfault rationale. */
    std::shared_ptr<void> req_writer_listener;
    std::shared_ptr<void> rep_reader_listener;
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
    return "wdndds/rpc/req/" + svc;
}
std::string rep_topic_name(const std::string& svc) {
    return "wdndds/rpc/rep/" + svc;
}

eprosima::fastdds::dds::Topic* get_or_create_topic(
    eprosima::fastdds::dds::DomainParticipant* participant,
    std::unordered_map<std::string, eprosima::fastdds::dds::Topic*>& cache,
    const std::string& topic_name,
    const std::string& type_name,
    eprosima::fastdds::dds::TypeSupport& ts)
{
    auto it = cache.find(topic_name);
    if (it != cache.end()) return it->second;

    if (!ts.empty())
    {
        ts.register_type(participant, type_name);
    }
    auto *topic = participant->create_topic(
        topic_name, type_name,
        eprosima::fastdds::dds::TOPIC_QOS_DEFAULT);
    if (topic) cache[topic_name] = topic;
    return topic;
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
        fprintf(stderr, "[wdndds_cshim] %s writer matched (+%d, -%d) total=%d\n",
                svc_.c_str(), info.current_count_change, info.current_count_change,
                info.current_count);
    }
private:
    std::string svc_;
};
class _ReaderMatchListener : public eprosima::fastdds::dds::DataReaderListener
{
public:
    _ReaderMatchListener(const std::string& svc) : svc_(svc) {}
    void on_subscription_matched(
            eprosima::fastdds::dds::DataReader*,
            const eprosima::fastdds::dds::SubscriptionMatchedStatus& info) override
    {
        fprintf(stderr, "[wdndds_cshim] %s reader matched (+%d, -%d) total=%d\n",
                svc_.c_str(), info.current_count_change, info.current_count_change,
                info.current_count);
    }
private:
    std::string svc_;
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
        qos.name() = participant_name ? participant_name : "pyrkaiweb";

        /* Transport selection:
         *   - Daemon (is_discovery_server=true): UDPv4 only. The daemon
         *     doesn't carry data, only discovery metadata, so SHM
         *     would be unused overhead. One descriptor matches the
         *     official FastDDS ServerApp pattern.
         *   - Client: SHM + UDPv4 + TCPv4. SHM for same-host data,
         *     UDPv4 for cross-host LAN, TCPv4 as cross-subnet fallback. */
        qos.transport().use_builtin_transports = false;
        if (is_discovery_server)
        {
            auto udpv4 = std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>();
            qos.transport().user_transports.push_back(udpv4);
        }
        else
        {
            /* Same-host data flows via SHM, cross-host data via
             * UDPv4. FastDDS 3.6 picks the right transport based on
             * the matched endpoint locators, so combining both is
             * safe. (Removing SHM completely broke same-host data
             * flow even via UDPv4 loopback — likely a FastDDS bug.) */
            qos.transport().user_transports.push_back(
                std::make_shared<eprosima::fastdds::rtps::SharedMemTransportDescriptor>());
            qos.transport().user_transports.push_back(
                std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>());
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
        if (internal->subscriber) {
            internal->impl->delete_subscriber(internal->subscriber);
            internal->subscriber = nullptr;
        }
        if (internal->publisher) {
            internal->impl->delete_publisher(internal->publisher);
            internal->publisher = nullptr;
        }
        for (auto& kv : internal->req_topics) {
            internal->impl->delete_topic(kv.second);
        }
        for (auto& kv : internal->rep_topics) {
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

/* Service (no rrpc — just caches topics for later use) -------------- */

wdndds_service_t *wdndds_service_create(
    wdndds_participant_t *p,
    const char *service_name)
{
    if (!p || !service_name) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);

    try
    {
        eprosima::fastdds::dds::TypeSupport req_type(
            new ComputeRequestPubSubType());
        eprosima::fastdds::dds::TypeSupport rep_type(
            new ComputeReplyPubSubType());

        std::string svc(service_name);
        auto *req_topic = wdndds_internal::get_or_create_topic(
            pin->impl, pin->req_topics,
            wdndds_internal::req_topic_name(svc),
            "ComputeRequest", req_type);
        auto *rep_topic = wdndds_internal::get_or_create_topic(
            pin->impl, pin->rep_topics,
            wdndds_internal::rep_topic_name(svc),
            "ComputeReply", rep_type);
        if (!req_topic || !rep_topic)
        {
            fprintf(stderr, "[wdndds_cshim] failed to create topics for %s\n", svc.c_str());
            return nullptr;
        }

        auto *s = new wdndds_internal::wdndds_replier_s();
        s->part = p;
        s->service_name = svc;
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
    delete reinterpret_cast<wdndds_internal::wdndds_replier_s *>(s);
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
        auto *req_topic = pin->req_topics[wdndds_internal::req_topic_name(svc)];
        auto *rep_topic = pin->rep_topics[wdndds_internal::rep_topic_name(svc)];

        eprosima::fastdds::dds::DataReaderQos reader_qos;
        reader_qos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
        reader_qos.history().kind = eprosima::fastdds::dds::KEEP_LAST_HISTORY_QOS;
        reader_qos.history().depth = 16;

        auto reader_listener = std::make_shared<_ReaderMatchListener>(svc + "/req");
        auto *reader = pin->subscriber->create_datareader(
            req_topic, reader_qos, reader_listener.get());
        if (!reader) return nullptr;

        eprosima::fastdds::dds::DataWriterQos writer_qos;
        writer_qos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
        writer_qos.history().kind = eprosima::fastdds::dds::KEEP_LAST_HISTORY_QOS;
        writer_qos.history().depth = 16;

        auto writer_listener = std::make_shared<_WriterMatchListener>(svc + "/rep");
        auto *writer = pin->publisher->create_datawriter(
            rep_topic, writer_qos, writer_listener.get());
        if (!writer) {
            pin->subscriber->delete_datareader(reader);
            return nullptr;
        }

        auto *r = new wdndds_internal::wdndds_replier_s();
        r->part = p;
        r->service_name = svc;
        /* Keep the listener shared_ptrs alive for the lifetime of
         * the replier — FastDDS holds raw pointers to the listeners
         * and calls them on matching events; if we let the
         * shared_ptrs go out of scope here, FastDDS has dangling
         * pointers and segfaults on the first matching event. */
        r->req_reader_listener = reader_listener;
        r->rep_writer_listener = writer_listener;
        r->req_reader = reader;
        r->rep_writer = writer;
        r->cb = cb;
        r->peer_cb = peer_cb;
        r->user = user;

        r->pump = std::thread([r]() {
            while (!r->stop.load())
            {
                ComputeRequest req;
                eprosima::fastdds::dds::SampleInfo info;
                auto rc = r->req_reader->take_next_sample(&req, &info);
if (rc == eprosima::fastdds::dds::RETCODE_OK)
                    fprintf(stderr, "[wdndds_cshim] req_reader got sample action=%d\n", req.action());
                if (rc == eprosima::fastdds::dds::RETCODE_OK)
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

                    char *out_reply = nullptr;
                    uint32_t out_len = 0;
                    int32_t cb_rc = r->cb(
                        r->service_name.c_str(),
                        payload_json.data(),
                        static_cast<uint32_t>(payload_json.size()),
                        0,
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

                    r->rep_writer->write(&reply);
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
    if (internal->part)
    {
        auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(internal->part);
        if (internal->rep_writer && pin->publisher) {
            pin->publisher->delete_datawriter(internal->rep_writer);
        }
        if (internal->req_reader && pin->subscriber) {
            pin->subscriber->delete_datareader(internal->req_reader);
        }
    }
    delete internal;
}

/* Requester (client) ----------------------------------------------- */

wdndds_requester_t *wdndds_requester_create(
    wdndds_participant_t *p,
    wdndds_service_t *s,
    int32_t timeout_ms)
{
    if (!p || !s) return nullptr;
    auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(p);
    auto *sin = reinterpret_cast<wdndds_internal::wdndds_replier_s *>(s);

    try
    {
        std::string svc = sin->service_name;
        auto *req_topic = pin->req_topics[wdndds_internal::req_topic_name(svc)];
        auto *rep_topic = pin->rep_topics[wdndds_internal::rep_topic_name(svc)];

        eprosima::fastdds::dds::DataWriterQos writer_qos;
        writer_qos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
        writer_qos.history().kind = eprosima::fastdds::dds::KEEP_LAST_HISTORY_QOS;
        writer_qos.history().depth = 16;

        auto writer_listener = std::make_shared<_WriterMatchListener>(svc + "/req");
        auto *writer = pin->publisher->create_datawriter(
            req_topic, writer_qos, writer_listener.get());
        if (!writer) return nullptr;

        eprosima::fastdds::dds::DataReaderQos reader_qos;
        reader_qos.reliability().kind = eprosima::fastdds::dds::RELIABLE_RELIABILITY_QOS;
        reader_qos.history().kind = eprosima::fastdds::dds::KEEP_LAST_HISTORY_QOS;
        reader_qos.history().depth = 16;

        auto reader_listener = std::make_shared<_ReaderMatchListener>(svc + "/rep");
        auto *reader = pin->subscriber->create_datareader(
            rep_topic, reader_qos, reader_listener.get());
        if (!reader) {
            pin->publisher->delete_datawriter(writer);
            return nullptr;
        }

        auto *r = new wdndds_internal::wdndds_requester_s();
        r->part = p;
        r->service_name = svc;
        r->req_writer = writer;
        r->rep_reader = reader;
        /* See wdndds_replier_s for rationale. */
        r->req_writer_listener = writer_listener;
        r->rep_reader_listener = reader_listener;
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
    if (internal->part)
    {
        auto *pin = reinterpret_cast<wdndds_internal::wdndds_participant_s *>(internal->part);
        if (internal->req_writer && pin->publisher) {
            pin->publisher->delete_datawriter(internal->req_writer);
        }
        if (internal->rep_reader && pin->subscriber) {
            pin->subscriber->delete_datareader(internal->rep_reader);
        }
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

        auto rc = internal->req_writer->write(&req);
        if (rc != eprosima::fastdds::dds::RETCODE_OK)
        {
            std::string err = "{\"ok\":false,\"error\":\"write rc=" +
                std::to_string((int)rc) + "\"}";
            *out_reply = wdndds_internal::copy_str(err);
            *out_reply_len = (uint32_t)err.size();
            return -1;
        }

        auto deadline = std::chrono::steady_clock::now() +
            std::chrono::milliseconds(timeout_ms > 0 ? timeout_ms : 10000);

        while (std::chrono::steady_clock::now() < deadline)
        {
            ComputeReply reply;
            eprosima::fastdds::dds::SampleInfo info;
            auto rc2 = internal->rep_reader->take_next_sample(&reply, &info);
            if (rc2 == eprosima::fastdds::dds::RETCODE_OK)
            {
                if (reply.request_id() == request_id)
                {
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
                continue;
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
    (void)r;
    return 0;
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
        cb_(guid_hex, info.participant_name.c_str(), discovered, user_);
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

/* DataWriter/DataReader listeners for matching events */

