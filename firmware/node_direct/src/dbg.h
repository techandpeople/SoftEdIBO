#pragma once
#include <Arduino.h>

// Compile-time toggleable debug printf — collapses to a no-op in release builds.

#ifdef DEBUG_BUILD
  #define DBG_PRINT(...)  Serial.printf(__VA_ARGS__)
  #define DBG_PRINTLN(s)  Serial.println(s)
#else
  #define DBG_PRINT(...)  ((void)0)
  #define DBG_PRINTLN(s)  ((void)0)
#endif
