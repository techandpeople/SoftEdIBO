# Main component. The shared ESP-NOW layer lives in firmware/common and is
# pulled in via the include path (header-only, no extra sources to compile).
idf_component_register(
    SRCS        "main.cpp"
    INCLUDE_DIRS "." "../../common"
    # esp_now.h ships inside esp_wifi; usb_serial_jtag.h is in its own driver
    # component (IDF 5.x). cJSON comes from json.
    REQUIRES    esp_wifi nvs_flash esp_netif esp_event esp_driver_usb_serial_jtag json
)
