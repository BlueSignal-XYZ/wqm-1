"""Regression: the radio must be able to transmit more than once per boot."""

from unittest.mock import MagicMock

_CMD_SET_DIO_IRQ_PARAMS = 0x08
_IRQ_TX_DONE = 0x0001


class TestTxDoneIrqReArmedEverysend:
    """receive() re-points DIO1 at RxDone; send() must put it back.

    Without this the radio transmits exactly once per process start: on a
    LoRaWAN device every uplink is followed by an RX window, so TxDone stops
    reaching DIO1 and every later send() times out reporting a TX failure for
    a packet that actually went out.
    """

    def _radio(self):
        import sys

        sys.modules.setdefault("lgpio", MagicMock())
        from radio.sx1262 import SX1262

        radio = SX1262()
        radio._wait_busy = lambda: None
        radio._tx_done_event = MagicMock()
        radio._tx_done_event.wait.return_value = True
        sent = []
        radio._cmd = lambda op, args=None: sent.append((op, list(args or [])))
        radio._xfer = lambda data: list(data)
        radio._sent = sent
        return radio

    def _dio1_masks(self, radio):
        """DIO1 mask is args[2:4] of every SetDioIrqParams call."""
        return [
            (a[2] << 8) | a[3]
            for op, a in radio._sent
            if op == _CMD_SET_DIO_IRQ_PARAMS and len(a) >= 4
        ]

    def test_send_arms_dio1_for_tx_done(self, mock_hardware):
        radio = self._radio()
        assert radio.send(b"\x01\x02\x03") is True
        assert _IRQ_TX_DONE in self._dio1_masks(radio)

    def test_send_after_receive_re_arms_tx_done(self, mock_hardware):
        radio = self._radio()
        radio.send(b"\x01")
        # Simulate what receive() does to DIO1 without driving a whole RX cycle.
        radio._cmd(_CMD_SET_DIO_IRQ_PARAMS, [0x02, 0x02, 0x02, 0x02, 0, 0, 0, 0])
        radio._sent.clear()

        assert radio.send(b"\x04\x05") is True
        masks = self._dio1_masks(radio)
        assert masks, "send() armed no DIO1 IRQ at all"
        assert masks[0] == _IRQ_TX_DONE, (
            f"send() left DIO1 on {masks[0]:#06x}; TxDone can never reach the host"
        )
