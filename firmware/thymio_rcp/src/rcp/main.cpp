/*
 * SoftEdIBO — Thymio RCP bring-up: RCP side (XIAO ESP32-C6), transport-agnostic.
 *
 * Responder half of the host <-> RCP link. Reads command lines from BOTH the
 * USB-Serial/JTAG port (Serial) AND the inter-board UART (Serial1), and replies
 * on the SAME channel the line arrived on. So one binary serves both worlds with
 * no rebuild and no mode jumper:
 *   - flashed solo on the C6's own USB -> type "PING 1" in the monitor, get "PONG 1"
 *   - wired to the S3 host over UART    -> the S3's PINGs get PONGed over TX/RX
 * "Reply on the source channel" is what "USB if present, else TX/RX" really needs,
 * and it is more robust than sniffing USB presence (no host => no bytes => we
 * simply never write to USB, so a disconnected USB never stalls us).
 *
 * For bring-up it answers "PING <n>" with "PONG <n>" (proves the C6 ran code on
 * the bytes — a shorted wire would echo "PING"). This is the seed of the real
 * radio co-processor: later the same dual-transport loop carries Thymio commands,
 * with the C6 speaking IEEE 802.15.4 to the Thymio. See docs/THYMIO_WIRELESS_CONTROL.md.
 *
 * Wiring (4 wires):
 *   C6 D6/TX (GPIO16) --> S3 D7/RX (GPIO44)
 *   C6 D7/RX (GPIO17) <-- S3 D6/TX (GPIO43)
 *   C6 GND ----------- S3 GND ;  C6 5V <---------- S3 5V
 *
 * Build/flash: pio run -e rcp_c6 -t upload.
 */
#include <Arduino.h>

static constexpr int      LINK_TX   = 16;      // D6 on the XIAO ESP32-C6
static constexpr int      LINK_RX   = 17;      // D7
static constexpr uint32_t LINK_BAUD = 115200;

// Service one transport: accumulate a line, and on end-of-line answer on the
// SAME stream. Each transport keeps its own line buffer (passed in by the caller).
static void pump(Stream &io, String &line) {
  while (io.available()) {
    char c = (char)io.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line.trim();
      if (line.startsWith("PING")) {
        io.printf("PONG%s\n", line.c_str() + 4);   // echo the same trailing payload
      }
      line = "";
    } else {
      line += c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // never block when no USB host is attached
  Serial1.begin(LINK_BAUD, SERIAL_8N1, LINK_RX, LINK_TX);
  delay(300);
  Serial.println("[rcp] C6 bring-up: PING -> PONG on both USB and UART");
}

void loop() {
  static String usb_line, uart_line;
  pump(Serial,  usb_line);    // USB-Serial/JTAG (solo testing)
  pump(Serial1, uart_line);   // inter-board UART (wired to the S3 host)
}
