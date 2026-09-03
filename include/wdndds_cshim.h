/* wdndds_cshim.h — public C ABI for wdndds RPC-DDS cshim.
 *
 * C ABI for the wdndds Python layer. Backed by FastDDS 3.6's rrpc
 * Service / Replier / Requester API + IDL-generated ComputeRequest /
 * ComputeReply types.
 *
 * Wire: standard DDS Service (one request topic + one reply topic per
 * service). Transport: SHM (local) + UDPv4 (LAN) + TCPv4 (cross-subnet).
 * Local data goes through shared memory; cross-host data goes through
 * TCPv4 (the JPEG-encoded attachment payload).
 *
 * Threading:
 *   - Replier auto-starts a std::thread pump on creation. User callback
 *     runs serialized; one request handled at a time, reply sent, then
 *     next request. The "server-side lock" maps to this pump loop.
 *   - Requester is sync-only; one call → one blocking send/recv.
 *     Concurrent callers can block on different Requester instances.
 *
 * Discovery (v0.5):
 *   - Default (no discovery_servers_csv): SIMPLE discovery + multicast.
 *   - With discovery_servers_csv + is_discovery_server=1: SERVER
 *     protocol on the daemon (used by wdndds_daemon).
 *   - With discovery_servers_csv + is_discovery_server=0: SUPER_CLIENT
 *     protocol pointing to the daemon (used by every wdnwebui
 *     process — main / compute_worker / im-bridge).
 */
#ifndef WDNDDS_CSHIM_H
#define WDNDDS_CSHIM_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque handles ------------------------------------------------------- */
typedef struct wdndds_participant_s    wdndds_participant_t;
typedef struct wdndds_service_s        wdndds_service_t;
typedef struct wdndds_replier_s        wdndds_replier_t;
typedef struct wdndds_requester_s      wdndds_requester_t;
typedef struct wdndds_topic_writer_s   wdndds_topic_writer_t;
typedef struct wdndds_topic_reader_s   wdndds_topic_reader_t;

/* Callbacks ------------------------------------------------------------ */

/* A request arrived at the Replier. The Python user fills `out_reply`
 * (a ctypes-allocated c_char_p; the cshim copies the bytes into the
 * ComputeReply and does NOT free the ctypes buffer — Python's ctypes
 * owns it). Returns 0 on success, negative on error. Threading: called
 * sequentially on the cshim's pump thread. */
typedef int32_t (*wdndds_request_cb_t)(
    const char *service_name,
    const char *payload,
    uint32_t    payload_len,
    int64_t     correlation_id,
    void       *user,
    char      **out_reply,
    uint32_t   *out_reply_len
);

/* Lifetime changed. Positive delta = newly matched, negative = lost. */
typedef void (*wdndds_peer_change_cb_t)(
    const char *service_name,
    int32_t     matched_delta,
    void       *user
);

/* Peer discovery callback. The cshim invokes this on every discovered
 * or lost remote participant.
 *
 * `ip` is the announcing participant's first UDPv4 unicast locator
 * (from ParticipantBuiltinTopicData.default_locators.unicast[0]),
 * formatted "a.b.c.d"; empty when the peer advertised no UDPv4 locator
 * (SHM-only or IPv6-only deployments). */
typedef void (*wdndds_peer_event_cb_t)(
    const char *guid_prefix,
    const char *participant_name,
    const char *ip,
    int32_t     discovered,   /* 1 = discovered, 0 = dropped */
    void       *user
);

/* Diagnostics ----------------------------------------------------------- */

int32_t wdndds_check_runtime(char **out_err);
void wdndds_free(char *p);

/* Participant ---------------------------------------------------------- */

/* Create a DomainParticipant.
 *
 * `discovery_servers_csv` is a newline-separated "host:port" list of
 * FastDDS discovery servers. Pass NULL/empty for default SIMPLE
 * discovery. When non-empty, `is_discovery_server` controls whether
 * this participant IS the server (SERVER protocol, listens) or a
 * client (SUPER_CLIENT protocol, connects). */
wdndds_participant_t *wdndds_participant_create(
    int32_t     domain_id,
    const char *participant_name,
    const char *discovery_servers_csv,
    int32_t     is_discovery_server
);

void wdndds_participant_destroy(wdndds_participant_t *p);

/* Return the GUID (47 hex chars) of this participant. */
int32_t wdndds_participant_guid(
    wdndds_participant_t *p,
    char                 *out_guid,
    uint32_t              out_guid_len
);

/* Empty for now (FastDDS 3.6 get_discovered_participants is stubbed).
 * Use wdndds_participant_set_listener for peer events. */
int32_t wdndds_participant_list_peers(
    wdndds_participant_t *p,
    char                **out_peers,
    uint32_t             *out_len
);

/* Install a peer-event listener. cb is invoked on every discovered
 * participant; pass NULL to remove. */
int32_t wdndds_participant_set_listener(
    wdndds_participant_t     *p,
    wdndds_peer_event_cb_t    cb,
    void                     *user
);

/* Service (no rrpc — just caches topics for later use) -------------- */

wdndds_service_t *wdndds_service_create(
    wdndds_participant_t *p,
    const char          *service_name
);

void wdndds_service_destroy(wdndds_service_t *s);

/* Replier (server) ----------------------------------------------------- */

wdndds_replier_t *wdndds_replier_create(
    wdndds_participant_t     *p,
    wdndds_service_t         *s,
    wdndds_request_cb_t       cb,
    wdndds_peer_change_cb_t   peer_cb,
    void                     *user
);

void wdndds_replier_destroy(wdndds_replier_t *r);

/* Requester (client) --------------------------------------------------- */

wdndds_requester_t *wdndds_requester_create(
    wdndds_participant_t *p,
    wdndds_service_t     *s,
    int32_t               timeout_ms
);

void wdndds_requester_destroy(wdndds_requester_t *r);

/* Synchronous RPC. Returns 0 on success, 1 on timeout, -1 on send. */
int32_t wdndds_requester_call(
    wdndds_requester_t *r,
    const char         *payload,
    uint32_t            payload_len,
    int32_t             timeout_ms,
    char              **out_reply,
    uint32_t           *out_reply_len
);

int32_t wdndds_requester_matched_count(wdndds_requester_t *r);

/* Topic (raw-bytes stream) --------------------------------------------- */

/* QoS preset selector for wdndds_topic_writer/reader_create:
 *   0 — BEST_EFFORT, history depth 4, volatile durability
 *   1 — RELIABLE,    history depth 16, volatile durability */
#define WDNDDS_TOPIC_QOS_BEST_EFFORT 0
#define WDNDDS_TOPIC_QOS_RELIABLE    1

/* Wire format: each chunk is a ComputeRequest carrying one ComputeAttachment
 * whose `bytes` field holds the raw payload. Topic name is used verbatim;
 * callers pick their own prefix (e.g. "wdndds/audio/tts_chunk/<model>"). */

/* Returns nullptr on failure. */
wdndds_topic_writer_t *wdndds_topic_writer_create(
    wdndds_participant_t *p,
    const char           *topic_name,
    int32_t               qos_kind);

/* Returns 0 on RETCODE_OK, non-zero otherwise. */
int32_t wdndds_topic_writer_write(
    wdndds_topic_writer_t *w,
    const char            *payload,
    uint32_t               payload_len);

void wdndds_topic_writer_destroy(wdndds_topic_writer_t *w);

/* Spawns a daemon pump thread; callback runs once per received chunk. */
wdndds_topic_reader_t *wdndds_topic_reader_create(
    wdndds_participant_t *p,
    const char           *topic_name,
    int32_t               qos_kind,
    void                (*callback)(const char *payload, uint32_t payload_len, void *user),
    void                 *user);

void wdndds_topic_reader_destroy(wdndds_topic_reader_t *r);

#ifdef __cplusplus
}
#endif

#endif  /* WDNDDS_CSHIM_H */
