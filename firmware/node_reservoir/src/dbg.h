#pragma once
#include <Arduino.h>

// Compile-time toggleable debug printf — collapses to a no-op in release.

#ifdef DEBUG_BUILD
  #define DBG_PRINT(...)  Serial.printf(__VA_ARGS__)
  #define DBG_PRINTLN(s)  Serial.println(s)
#else
  #define DBG_PRINT(...)  ((void)0)
  #define DBG_PRINTLN(s)  ((void)0)
#endif

// Always-on Serial output (used for TODO/ERROR messages that the user must
// see when verifying autodetect against the physical PCB).
#define LOG(...)   do { Serial.printf(__VA_ARGS__); } while (0)
