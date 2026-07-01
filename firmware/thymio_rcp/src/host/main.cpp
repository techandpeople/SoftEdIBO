/*
 * SoftEdIBO — Thymio RCP bring-up: HOST side (XIAO ESP32-S3).
 *
 * Validates the host <-> RCP UART link before any 802.15.4 / Thymio work.
 * The S3 (host) periodically sends "PING <n>" over the inter-board UART and
 * prints whatever the C6 (RCP) replies. A correct reply is "PONG <n>", which
 * proves the C6 received the line AND ran code on it — not just a shorted-wire
 * loopback.
 *
 * Once this passes, the host firmware grows into the gateway role: it routes
 * Thymio-targeted commands from the PC to the C6 over this same UART, while the
 * C6 speaks IEEE 802.15.4 to the Thymio's wireless module.
 *
 * Wiring (4 wires) — see docs/THYMIO_WIRELESS_CONTROL.md:
 *   S3 D6/TX (GPIO43) --> C6 D7/RX (GPIO17)
 *   S3 D7/RX (GPIO44) <-- C6 D6/TX (GPIO16)
 *   S3 GND ----------- C6 GND       (common ground, required)
 *   S3 5V  ----------> C6 5V        (power the C6 from the single USB cable)
 *
 * Build/flash: pio run -e host_s3 -t upload ; then pio device monitor.
 */
#include <Arduino.h>

static constexpr int      LINK_TX   = 43;      // D6 on the XIAO ESP32-S3
static constexpr int      LINK_RX   = 44;      // D7
static constexpr uint32_t LINK_BAUD = 115200;  // bring-up speed; raise later
static constexpr uint32_t REPLY_TIMEOUT_MS = 1000;

void setup() {
  Serial.begin(115200);                                   // USB-Serial/JTAG to PC
  Serial1.begin(LINK_BAUD, SERIAL_8N1, LINK_RX, LINK_TX);  // inter-board link
  delay(300);
  Serial.println("[host] S3 bring-up: pinging the C6 RCP over UART");
}

void loop() {
  static uint32_t n = 0;
  Serial1.printf("PING %lu\n", (unsigned long)n);

  // Collect one reply line, or give up after REPLY_TIMEOUT_MS.
  String reply;
  bool got_line = false;
  const unsigned long start = millis();
  while (!got_line && millis() - start < REPLY_TIMEOUT_MS) {
    while (Serial1.available()) {
      char c = (char)Serial1.read();
      if (c == '\r') continue;
      if (c == '\n') { got_line = true; break; }
      reply += c;
    }
  }

  if (got_line) {
    Serial.printf("[host] PING %lu -> \"%s\"  %s\n",
                  (unsigned long)n, reply.c_str(),
                  reply.startsWith("PONG") ? "OK" : "(unexpected reply)");
  } else {
    Serial.printf("[host] PING %lu -> NO REPLY "
                  "(check: TX<->RX crossed? common GND? C6 powered & flashed?)\n",
                  (unsigned long)n);
  }

  n++;
  delay(1000);
}
