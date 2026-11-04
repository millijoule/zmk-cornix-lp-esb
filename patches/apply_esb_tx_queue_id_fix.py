#!/usr/bin/env python3
"""Apply a narrowly scoped fix to the pinned badjeff ESB transport.

The upstream module queues payloads but previously kept only one global
message ID, updated at enqueue time. When a second packet arrived while the
first was in flight, its ID could replace the in-flight packet's ID.
This patch carries the ID alongside each queued payload and activates it only
when that payload is handed to the Nordic ESB driver.
"""

from pathlib import Path
import sys


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return source.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_esb_tx_queue_id_fix.py PATH_TO_APP_ESB_C")

    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")

    source = replace_once(
        source,
        """struct retry_entry {
    uint16_t msg_id;
    uint8_t left;
    uint8_t max;
    struct esb_payload payload;
};""",
        """struct tx_queue_entry {
    struct esb_payload payload;
    uint16_t msg_id;
};

struct retry_entry {
    uint16_t msg_id;
    uint8_t left;
    uint8_t max;
    struct esb_payload payload;
};""",
        "add per-payload queue metadata",
    )

    source = replace_once(
        source,
        """K_MSGQ_DEFINE(m_msgq_tx_payloads, sizeof(struct esb_payload), 
              CONFIG_ZMK_SPLIT_ESB_PROTO_MSGQ_ITEMS, 4);""",
        """K_MSGQ_DEFINE(m_msgq_tx_payloads, sizeof(struct tx_queue_entry),
              CONFIG_ZMK_SPLIT_ESB_PROTO_MSGQ_ITEMS, 4);""",
        "resize TX queue item",
    )

    tx_cases_start = source.index("        case ESB_EVENT_TX_SUCCESS:")
    tx_cases_end = source.index("        case ESB_EVENT_RX_RECEIVED:", tx_cases_start)
    source = (
        source[:tx_cases_start]
        + """        case ESB_EVENT_TX_SUCCESS: {
            uint16_t completed_msg_id = m_current_tx_msg_id;
            m_current_tx_msg_id = 0;
            if (completed_msg_id != 0) {
                remove_retry_entry_by_msg_id(completed_msg_id);
            }

            // Forward an event to the application.
            m_event.evt_type = APP_ESB_EVT_TX_SUCCESS;
            m_callback(&m_event);
            pull_packet_from_tx_msgq();
            break;
        }
        case ESB_EVENT_TX_FAILED: {
            uint16_t failed_msg_id = m_current_tx_msg_id;
            uint8_t retry_left = decrement_retry_by_msg_id(failed_msg_id);
            LOG_WRN("Retry left for msg %d: %d", failed_msg_id, retry_left);

            // Re-insert the exact failed payload with its own message ID.
            bool dispose_msg = !retry_left;
            if (retry_left > 0) {
                struct tx_queue_entry retry_payload = {
                    .msg_id = failed_msg_id,
                };
                if (get_retry_payload_by_msg_id(failed_msg_id, &retry_payload.payload)) {
                    int requeue_ret =
                        k_msgq_put(&m_msgq_tx_payloads, &retry_payload, K_NO_WAIT);
                    if (requeue_ret == -ENOMSG) {
                        LOG_DBG("Msgq full, cannot re-queue payload from retry table");
                        dispose_msg = true;
                    }
                } else {
                    LOG_ERR("Failed to get payload from retry table for retry");
                    dispose_msg = true;
                }
            }
            if (dispose_msg && failed_msg_id != 0) {
                remove_retry_entry_by_msg_id(failed_msg_id);
                LOG_DBG("Disposed payload from retry table after too many failures");
            }

            // The next packet selected from the queue will set its own ID.
            m_current_tx_msg_id = 0;

#if IS_ENABLED(CONFIG_ZMK_SPLIT_ESB_RF_CH_HOP)
            esb_rf_ch_hop();
#endif /* IS_ENABLED(CONFIG_ZMK_SPLIT_ESB_RF_CH_HOP) */

            esb_flush_tx();
            m_event.evt_type = APP_ESB_EVT_TX_FAIL;
            m_callback(&m_event);
            pull_packet_from_tx_msgq();
            break;
        }
""".replace("\n", "\n")
        + source[tx_cases_end:]
    )

    pull_start = source.index("static int pull_packet_from_tx_msgq(void) {")
    pull_end = source.index("\nint zmk_split_esb_init", pull_start)
    new_pull = """static int pull_packet_from_tx_msgq(void) {
    int ret = 0;
    int esb_ret;
    struct tx_queue_entry tx_entry;
    static uint8_t que_was_fulled = 0;

    if (!esb_is_idle() && m_mode == APP_ESB_MODE_PTX) {
        return -EBUSY;
    }

    if (k_msgq_peek(&m_msgq_tx_payloads, &tx_entry) == 0) {
        ret = esb_write_payload(&tx_entry.payload);

        if (ret == -ENOMEM) {
            // The ESB hardware FIFO is full. Keep the queued item unless the
            // existing FIFO recovery threshold says it must be discarded.
            que_was_fulled++;
            esb_flush_tx();
            if (que_was_fulled >= ESB_TX_FIFO_REQUE_MAX) {
                k_msgq_get(&m_msgq_tx_payloads, &tx_entry, K_NO_WAIT);
                remove_retry_entry_by_msg_id(tx_entry.msg_id);
            }

        } else if (ret == -EMSGSIZE) {
            LOG_WRN("esb_tx_fifo: tx_payload size too large (%d) > CONFIG_ESB_MAX_PAYLOAD_LENGTH (%d)",
                    tx_entry.payload.length, CONFIG_ESB_MAX_PAYLOAD_LENGTH);
            k_msgq_get(&m_msgq_tx_payloads, &tx_entry, K_NO_WAIT);
            remove_retry_entry_by_msg_id(tx_entry.msg_id);

        } else if (ret) {
            LOG_WRN("esb_write_payload failed (%d)", ret);
            uint8_t retry_left = get_retry_left_by_msg_id(tx_entry.msg_id);
            k_msgq_get(&m_msgq_tx_payloads, &tx_entry, K_NO_WAIT);
            if (retry_left > 0) {
                LOG_WRN("Payload dequeued, retry_left=%d", retry_left);
                // A later TX failure callback will requeue the active packet.
                // Do not associate this queued item with another packet's ID.
            } else {
                remove_retry_entry_by_msg_id(tx_entry.msg_id);
            }

        } else {
            // The message ID must follow the packet actually selected for TX,
            // not the newest packet added to the software queue.
            m_current_tx_msg_id = tx_entry.msg_id;

            if (m_mode == APP_ESB_MODE_PTX) {
                esb_ret = esb_start_tx();
                if (esb_ret == -EBUSY) {
                    LOG_DBG("ESB busy, will retry on next event");
                    m_current_tx_msg_id = 0;
                    return -EBUSY;
                } else if (esb_ret == -ENODATA) {
                    LOG_DBG("ESB TX FIFO empty");
                    m_current_tx_msg_id = 0;
                    return 0;
                } else if (esb_ret < 0) {
                    LOG_ERR("esb_start_tx failed (%d)", esb_ret);
                    m_current_tx_msg_id = 0;
                    return esb_ret;
                }
            }

            k_msgq_get(&m_msgq_tx_payloads, &tx_entry, K_NO_WAIT);
            que_was_fulled = 0;
        }
    }

    return ret;
}
"""
    source = source[:pull_start] + new_pull + source[pull_end:]

    send_start = source.index("int zmk_split_esb_send(app_esb_data_t *tx_packet) {")
    send_end = source.index("\n#if IS_ENABLED(CONFIG_ZMK_SPLIT_ESB_USE_TIMESLOT)", send_start)
    new_send = """int zmk_split_esb_send(app_esb_data_t *tx_packet) {
    int ret = 0;
    struct esb_payload tx_payload = {
        .pipe = tx_packet->pipe,
#if IS_ENABLED(CONFIG_ZMK_SPLIT_ESB_PROTO_TX_ACK)
        .noack = false,
#else
        .noack = true,
#endif
        .length = tx_packet->len,
    };
    struct tx_queue_entry tx_entry = {
        .msg_id = tx_packet->msg_id,
    };

    memcpy(tx_payload.data, tx_packet->data, tx_packet->len);
    tx_entry.payload = tx_payload;

    if (!tx_payload.length) {
        LOG_WRN("bypass queuing null payload");
        return 0;
    }

    ret = k_msgq_put(&m_msgq_tx_payloads, &tx_entry, K_NO_WAIT);

    if (ret == 0) {
        int idx = add_retry_entry(tx_packet->msg_id, tx_packet->max_retry, &tx_payload);
        if (idx < 0) {
            LOG_WRN("No retry-table slot for message %d", tx_packet->msg_id);
        }
        m_msgq_full_last_time = 0;
    } else if (ret == -ENOMSG) {
        uint32_t now = k_uptime_get_32();
        if (!m_msgq_full_last_time) {
            m_msgq_full_last_time = now;
        }
        if (now - m_msgq_full_last_time > CONFIG_ZMK_SPLIT_ESB_MSGQ_FULL_TIMEOUT_MS) {
            LOG_WRN("Msgq full for %dms, clearing msgq and retry table",
                    CONFIG_ZMK_SPLIT_ESB_MSGQ_FULL_TIMEOUT_MS);
            k_msgq_purge(&m_msgq_tx_payloads);
            clear_retry_table();
            m_current_tx_msg_id = 0;
            if (m_active) {
                esb_flush_tx();
            }
            m_msgq_full_last_time = 0;
        }
    } else {
        LOG_DBG("Failed to queue esb tx_payload_q (%d)", ret);
    }

    if (m_active) {
        pull_packet_from_tx_msgq();
    }
    return ret;
}
"""
    source = source[:send_start] + new_send + source[send_end:]

    path.write_text(source, encoding="utf-8", newline="\n")
    print(f"Applied per-packet ESB TX message-ID fix to {path}")


if __name__ == "__main__":
    main()
