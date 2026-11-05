# Component Inventory

Atech.dev modular hardware (Atech ApS). Modules plug into motherboard ports. Each port has 6 pins: **HV · 5V · 3.3V · GND · Data A · Data B**. Every module documented here uses only 3.3V, GND, Data A and Data B; HV and 5V are not connected. Single-width modules take 1 port. Double-width modules take 2 adjacent ports, so their pins are listed as A1/B1 (first port) and A2/B2 (second port).

## Our kit (counted from photos, 2026-11-05)

| Qty | Module | Where | Notes |
|---|---|---|---|
| 1 | 14-Port Motherboard | — | Port 7 is free; USB-C sits in the USB port at the bottom of the centre column |
| 1 | USB-C | on board | Powering and programming the board |
| 3 | Button | on board, left column | Labelled **V2.0**, so the RGB indicator on Data B works |
| 3 | Light Grid | on board, left column | Lit and working |
| 1 | Distance Sensor | on board, top right | |
| 1 | Temperature & Humidity | on board, right column | Labelled **V1.1** |
| 1 | Screen (small, 160×80) | on board, right column | Takes 2 ports |
| 1 | Microphone | on board, bottom right | Takes 2 ports |
| 1 | Orientation Sensor | loose | |
| 1 | Knob | loose (tray) | Takes 2 ports |
| 1 | Speaker | loose (tray) | Takes 2 ports |
| 4 | DC Motor | loose (tray) | |

That's **19 modules plus the board**, worth **3,541 NOK** at list price.

**Not in the kit:**
- 10-Port Motherboard
- Big Screen
- Camera
- Micro-SD Card
- CO₂ Sensor
- Soil Sensor
- Power Jack
- Robot Arm Integrator
- Load Cell (listed as "coming soon")

**Port budget:**
- All 19 modules together need **21 ports**, more than one board has, so pick a subset per build.
- 12 ports are in use right now and port 7 is free.
- The loose modules need 9 more ports: Orientation 1, Knob 2, Speaker 2 and the 4 Motors 4.

**Power:**
- There's no Power Jack, so everything runs from USB-C, which supplies up to 3 A.
- 4 DC Motors (~2 A) plus 3 Light Grids (~1.6 A) would exceed that. Don't run all motors and all grids at full load together.

## At a glance

| Module | Owned | Price (NOK) | Category | Ports | Interface | Current | Size (mm) | Chip | Driver |
|---|---|---|---|---|---|---|---|---|---|
| 14-Port Motherboard | **1** | 599 | Board | 14 | USB-C, WiFi, BLE 5.0 | — | — | ESP32-S3 | — |
| 10-Port Motherboard | 0 | 599 | Board | 10 (9 usable) | USB-C, WiFi, BLE 5.0 | — | 40 × 60 | ESP32-S3-WROOM-1 | — |
| USB-C | **1** | 109 | Power | 1 | USB 2.0 | up to 3 A out | — | — | — |
| Power Jack | 0 | 189 | Power | 1 (overhangs) | I2C (current monitor) | eFuse | single-width | — | — |
| Button ⭐ | **3** | 109 | Input | 1 | Digital | ~20 mA | 19.5 × 19.5 × 10 | — | `ButtonModule` |
| Knob | **1** | 189 | Input | 2 | Digital | ~125 mA | 39.5 × 39.5 × 38.1 | EC11 | `RotaryEncoder` |
| Temperature & Humidity | **1** | 189 | Sensor | 1 | I2C 0x38 | ~1 mA | 19.5 × 19.5 × 9.5 | AHT20 | `AHT20` |
| CO₂ Sensor | 0 | 469 | Sensor | 1 | I2C 0x62 | ~18 mA | 19.5 × 19.7 × 10.4 | SCD40 | `SCD40` |
| Distance Sensor | **1** | 189 | Sensor | 1 | I2C 0x29 | ~20 mA | 19.5 × 19.5 × 9.5 | VL53L5CX | `VL53L5CX_Sensor` |
| Orientation Sensor | **1** | 189 | Sensor | 1 | I2C 0x68 | ~4 mA | 19.5 × 19.5 × 9.5 | ICM-40608 / LSM6DSOX | `ICM40608` |
| Soil Sensor | 0 | 189 | Sensor | 1 | I2C 0x37 | ~25 mA | not stated (70 mm probe blade) | CH32V003 | `SoilSensor` |
| Load Cell | 0 | coming soon | Sensor | 1 | Digital (clock + data) | — | single-width | HX711 | — |
| Microphone | **1** | 239 | Sensor/Audio | 2 | I2S | ~5 mA | 39.5 × 19.5 × 10.8 | ICS-43434 | `Microphone` |
| Screen | **1** | 189 | Display | 2 | SPI | ~25 mA | 39.5 × 19.5 × 9.6 | ST7735S | `ST7735_TFT` |
| Big Screen | 0 | 239 | Display | 2 (overhangs) | SPI | ~40 mA | 39.5 × 59.7 × 9.5 | ST7789 | `ST7789_TFT` |
| Light Grid ⭐ | **3** | 109 | Light | 1 | Digital | ~540 mA | 19.5 × 19.5 × 9.5 | WS2812B | `NeoPixelGrid` |
| Speaker | **1** | 239 | Audio | 2 | I2S | ~200 mA | 40 × 40 × 20.2 | MAX98357A | `Speaker` |
| DC Motor | **4** | 189 | Actuator | 1 (overhangs) | Digital (PWM) | ~500 mA | 19.5 × 48.3 × 19.1 | DRV8833 | `DCMotor` |
| Robot Arm Integrator | 0 | 189 | Actuator | 1 (overhangs) | UART half-duplex 1 Mbps | up to 3 A (fused) | single-width | — | — |
| Micro-SD Card | 0 | 109 | Storage | 2 | SPI | ~100 mA | 39.5 × 19.7 × 9.5 | — | `MicroSDCard` |
| Camera | 0 | 469 | Vision | 2 | SPI | ~250 mA | 39.5 × 19.7 × 14.4 | OV5640 + ESP32-S3 | `AtechCamera` |

⭐ = Atech's recommended default input (Button) and output (Light Grid). Prices are from the Atech shop listing.

---

## Motherboards

**14-Port Motherboard.** ESP32-S3 with WiFi and Bluetooth 5.0 LE, powered at 5V over USB-C. It has 14 module ports, a dedicated USB port for programming, and a reset port where you can fit a button to restart the device. It takes several double-width modules at once. Best for complex, multi-sensor, full-featured builds and advanced prototyping.

**10-Port Motherboard.** ESP32-S3-WROOM-1 (8 MB flash, 2 MB PSRAM) on a 40 × 60 mm board with ports on both faces:
- Six ports on top, along a central spine, opening left and right.
- Four ports underneath, opening toward the edges.

Nine ports take modules and one carries USB. It also has 14 addressable LEDs and an HV rail rated to 30V. It uses the same firmware and modules as the 14-port board. Best for wearables, handhelds, small enclosures, modules entering from four sides, and battery or higher-voltage builds.

**ESP32-S3 limits that affect module choice:**
- 4 hardware pulse counters (PCNT), so at most 4 Knobs.
- 2 hardware SPI hosts, shared by the Camera, Micro-SD and screens.

## Power & Connectivity

**USB-C.** Reversible USB 2.0 connector, 5V, up to 3 A, no drivers needed.
- In the motherboard's dedicated USB port, it is used for flashing and updating firmware and for data transfer.
- In any other port, it only powers the board.

**Power Jack.** DC barrel jack that takes a 12V supply and feeds the board's 12V rail through an electronic fuse. An I2C current monitor reports how much the board is drawing, and a status LED shows the rail state. It is single-width and extends past the board edge.
- Required by the Robot Arm Integrator.
- Use it for motor and servo projects, bench power without USB, permanent installations and power monitoring.

## Input

### Button ⭐
Momentary push button with an indicator light (blue; an RGB LED on V2.0).
- **Pins:** A = switch, active low (pressed = LOW) · B = RGB indicator LED (V2.0 only)
- **API:** `isPressed()` (level) · `wasPressed()` / `wasReleased()` (edge, true once per event) · `update()`
- **Rules:**
  - Call `update()` at the start of every loop, or edge detection never fires.
  - Don't use `wasPressed()` in a `while()`. Use `isPressed()` for hold detection.
  - The driver has **no software debounce**, despite the spec. One bouncing press measured three edges. For toggles and counters, ignore edges within ~40 ms of the last accepted one.
  - Instances are named by port number (`button_2` = port 2), not numbered in order.
- **UI idea:** an indicator that flashes on each press, with a press counter.

### Knob
Rotary encoder with a push button and an LED ring. The spec says 18 click-steps and 6 LEDs; the driver doc says 15 steps and a 12-LED NeoPixel ring.
- **Pins:** A1 = encoder CLK · B1 = encoder DT · A2 = button (active low) · B2 = NeoPixel ring data
- **API:**
  - Position: `getPosition()` (cumulative int32) · `setPosition(pos)` · `resetPosition()` · `getDirection()` (1 = CW, -1 = CCW, 0 = none) · `wasRotatedCW()` / `wasRotatedCCW()` (edge)
  - Button: `isPressed()` · `wasPressed()` (edge)
  - Settings: `setAcceleration(enabled, maxMultiplier=5)`, on by default
  - Ring: `enableRing(bool)` · `setRingColor(r,g,b)` (default white) · `setRingBrightness(0–255, default ~50)` · `setRingPosition(0.0–12.0)`, which goes counter-clockwise from 12 o'clock; pass a negative value to re-couple the ring to the knob
- **Rules:**
  - Call `update()` every loop. It reads the encoder, polls the button and repaints the ring, and ring changes don't appear until it runs.
  - Steps are counted in hardware (PCNT), so none are lost, but a 2–5 ms loop delay keeps the ring snappy; 15–20 ms visibly lags.
  - At most 4 Knobs per board.
  - Detect changes with `if (pos != lastPos)`.
- **Uses:** volume, menus, parameter tuning, level selection.

## Sensors

All I2C sensors use **A = SDA, B = SCL**, unless noted. They run a background task, so their getters return cached values instantly and never block.

### Temperature & Humidity (AHT20)
Measures -40 to +125 °C and 0–100 %RH.
- **API:** `readTemperature()` (°C) · `readHumidity()` (%RH)
- **Rules:**
  - The sensor measures every ~2 s, so don't read faster than every 500 ms.
  - The first read may briefly be 0.
- **Uses:** weather stations, climate control, smart home.

### CO₂ Sensor (Sensirion SCD40)
True NDIR CO₂ measurement (not estimated), typically 400–2000 ppm. Temperature and humidity come along as calibration byproducts.
- **API:** `readCO2()` (uint16 ppm) · `readTemperature()` · `readHumidity()` · `isReady()`
- **Rules:**
  - Readings update every ~5 s. `readCO2()` returns 0 until the first one, ~5 s after boot, so guard with `isReady()`.
  - Don't poll faster than every 500 ms.
- **Air-quality bands:** <800 ppm fresh · 800–1200 acceptable · >1500 poor (sleepy/distracted) · >2000 uncomfortable
- **Uses:** indoor air quality, ventilation alerts, grow rooms, occupancy estimation.

### Distance Sensor (VL53L5CX)
8×8 multi-zone time-of-flight sensor: 64 distance readings, range up to 4 m, ranging at 15 Hz, unaffected by ambient light.

Note that the driver doc says SCL is on A and SDA on B, which is the opposite of the pin table.
- **API:** `getDistance(zone 0–63)` (mm) · `getMinDistance()` · `getMaxDistance()` · `isReady()`
- **Rules:**
  - With nothing in range, readings return the sentinel **32767**, not 0 or -1.
  - For "do X when something is in front", use an edge-triggered presence detector with hysteresis: enter when < 300 mm, leave when > 400 mm.
  - Don't add a cooldown, and respond within one reading (~70 ms).
- **Uses:** obstacle detection, proximity, people counting, gestures.

### Orientation Sensor (6-axis IMU)
3-axis accelerometer (±2 g), 3-axis gyroscope (±250 °/s) and on-die temperature, polled at 100 Hz. The driver auto-detects which chip is fitted (ICM-40608 at 0x68 or LSM6DSOX at 0x6A).
- **API:**
  - Prefer these derived values: `getPitch()` (-90..90°) · `getRoll()` (-180..180°) · `getTiltAngle()` (0 = face up, 180 = face down) · `getOrientation()` / `getOrientationName()` (face_up/face_down/edge_left/right/front/back/unknown) · `wasShaken()` (edge, >1.8 g spike) · `setShakeThreshold(g)` (~1.4 for taps, ~2.5 for vigorous shakes)
  - Raw: `readAccelX/Y/Z()` (g) · `readGyroX/Y/Z()` (°/s) · `readTemperature()` · `calibrateGyro()` (once in setup, while still) · `setMountRotation(deg)`
- **Rules:**
  - Pitch and roll come from the accelerometer, so they are noisy while the board is accelerating and assume it lies flat.
  - A real shake gesture is 2–3 `wasShaken()` events within ~500 ms.
  - Hold `face_down` for ~200 ms before acting on it.
  - Treat the board as "moving" when total gyro magnitude is > 30 °/s.
- **Uses:** game controllers, tilt and gesture control, motion logging.

### Soil Sensor
Two-prong galvanic probe on a 70 mm blade. A CH32V003 co-processor on the module does the measuring, and a status LED shows the moisture level by colour.
- **API:**
  - Readings: `readMoisture()` (0–100 %) · `readConductance()` (µS) · `readRaw()` (counts; **high = dry**) · `hasReading()`
  - Calibration: `calibrateDry()` (probe in air) · `calibrateWet()` (prongs in water) · `setCalibration(dryRaw, wetRaw)` (requires dry > wet)
  - State: `isDry()` = probe open or in air · `isSaturated()` = standing water or a short
  - Tuning: `setSmoothing()` · `setSlewThreshold()` · `setSamplePeriod(ms)`
  - LED: `setStatusColor()` · `setStatusOff()` · `setStatusAuto()`
- **Rules:**
  - It is I2C, not analog. `analogRead()` won't work.
  - The percentage means nothing until both calibration points are recorded.
  - Smoothing is done on the module, so don't filter again on the host.
  - For a probe left in soil for months, use `setSamplePeriod(1000)` or slower.
  - `isDry()` does not mean the plant needs water.
- **Uses:** automated watering, garden and greenhouse monitoring.

### Load Cell (HX711)
24-bit load-cell amplifier with a header for any 4-wire bar or button load cell, from a few hundred grams up to 200 kg. Tare and calibrate from firmware; it reads in grams at up to 80 samples/s.
- **Uses:** scales and portion control, stock-level tracking (e.g. a shelf), presence by weight (e.g. a bed or a dog bowl), force experiments.

### Microphone (ICS-43434 I2S MEMS)
Low-noise MEMS microphone with high signal-to-noise ratio, 24-bit output at 16 kHz.
- **Pins:** A1 = LR (channel select) · B1 = WS · A2 = SCK · B2 = SD
- **API:**
  - Levels: `getSoundLevel()` (RMS 0–32767) · `isSoundDetected(threshold=500)` · `getPeakAmplitude()` (resets on read; use for claps and beats) · `getSamples(buf)`
  - Recording: `startRecording()` / `stopRecording()` (up to ~10 s, stored in heap) · `isRecording()` · `getRecording()` (returns `const int16_t*`) · `getRecordingLength()`
  - Setup: `setChannel()`
- **Rules:**
  - Poll the level periodically, not in a tight loop.
  - To play a recording back, pass it to `speaker.writeSamples()`.
- **Uses:** voice recording and commands, sound detection, audio monitoring.

## Displays & Light

### Screen (1.8" ST7735S, 160×80)
65K colours (RGB565) over 40 MHz SPI.
- **Pins:** A1 = DC · B1 = CS · A2 = MOSI · B2 = SCLK
- **API:** `displayText(text,x,y,size,color)` · `clear()` · `fillScreen()` · `drawPixel` · `drawLine` · `drawRect` / `fillRect` · `drawCircle` / `fillCircle` · `setCursor` + `print` · `setFont`
- **Drawing modes:**
  - **Canvas** (default) for dashboards: `clear()` → draw → `display()`, flicker-free.
  - **Direct** (`setDirectMode(true)` in setup) for games and animation: erase old positions with small `fillRect`s. Never call `fillScreen()`, `clear()` or `display()` in the loop; use `fillScreen()` once per screen change.
- **Text:**
  - `print()` takes one argument. Use `setCursor(x,y)` first to position it.
  - For anything larger than size 1, use smooth fonts: FreeSans 9/12pt or FreeSansBold 9/12/18/24pt. Call `setFont(&...)` then `setTextSize(1)`. With these fonts, Y is the text **baseline**. `setFont(NULL)` resets to the default font.
- **Colours:** `ST7735_BLACK/WHITE/RED/GREEN/BLUE/YELLOW/CYAN/MAGENTA/ORANGE`
- **Uses:** UI, status displays, data visualisation, small games.

### Big Screen (2.0" IPS ST7789, 320×240)
Same API and wiring as the Screen with four times the pixels, plus `drawRoundRect/fillRoundRect` · `drawTriangle/fillTriangle` · `drawEllipse/fillEllipse` · `textWidth()` · `lineHeight()` · `textBounds()` · `print(float, decimals)`. The panel overhangs the board edge, and the backlight is always on with no brightness control.
- **Speed:** a full frame takes ~31 ms (≈20–25 fps), so rate-limit redraws to 33–50 ms.
- **Modes:**
  - Canvas needs a 150 KB framebuffer and falls back to direct mode if it can't allocate one (check `isDirectMode()`).
  - Use direct mode only for small sprites over a static background.
- **Rotation:** 1/3 = landscape, 0/2 = portrait. Rotation clears the canvas, so set it first in setup.
- **SPI:** uses a hardware SPI host if one is free, otherwise bit-bangs, which is slower (check `isSoftwareSpi()`).
- **Colours:** `ST7789_TFT::COLOR_*` or raw RGB565.

### Light Grid ⭐ (3×3 WS2812B)
Nine individually addressable RGB LEDs, 16M colours each. The driver writes to both module revisions every frame (V1 RGB on line A, V2 RGBW on line B), so the same code works on either.
- **API:** `setPixel(0–8, r,g,b)` (row-major, 0 = top-left) · `setAll` · `setRow(0–2)` · `setColumn(0–2)` · `clear()` · `show()` · `setBrightness()` (**capped at 51**) · `getPixelColor()` (returns 0x00RRGGBB)
- **Rules:**
  - Nothing changes on the LEDs until you call `show()`.
  - `show()` takes ~5 ms, so only call it when colours actually change, and throttle to ~30 Hz for knob- or slider-driven input.
  - For animations, use 20–50 ms between frames.
  - Don't address the white channel directly.
- **Uses:** status indicators, animations, visual feedback.

## Audio & Motion

### Speaker (MAX98357A, 3 W Class D)
I2S amplifier with a built-in DAC.
- **Pins:** A1 = LRCLK · B1 = BCLK · A2 = not connected · B2 = DIN
- **API:**
  - Tones: `playTone(freq, ms)` (non-blocking; ≤0 ms = play until `stop()`)
  - Notes and melodies: `playNote(freq, ms)` (adds a 15% gap, auto-queues up to 16 notes; `NOTE_REST` = 0) · `playChord(freqs[], n, ms)` (up to 6 notes) · `playRTTTL("name:d=4,o=5,b=140:c,e,g")` (runs in background) · `rest`
  - Control: `stop()` · `setVolume(0.0–1.0)`
  - Raw audio: `writeSamples(buf, len)` · `playPCMBase64()`
- **Rules:**
  - **No sounds on boot** (it's the #1 user complaint).
  - Play sounds only in response to events, using guards, flags or cooldowns. Stay silent when idle.
  - For feedback, use short 2–4 note motifs, not bare tones.
  - Stick to `NOTE_C4`–`NOTE_B5` (≈260–990 Hz). Above C6 gets piercing; never go above 2000 Hz.
  - Call `setVolume(0.4)` in setup (1.0 clips).
  - Keep tones ≤ 2 s.
  - Never use Arduino `tone()` or `noTone()`.
- **MP3:** the browser or app decodes the file to 16 kHz mono PCM and streams it with `play_pcm`. Don't add an MP3 decoder library to the device; it breaks the build.
- **Uses:** alarms, melodies, audio and voice playback.

### DC Motor (DRV8833 H-bridge)
High-torque motor for deliberate movements, with forward, reverse and speed control. The module extends beyond its port.

Note that the driver is wired with IN1 on line B and IN2 on line A, the opposite of the module's own labels.
- **API:** `forward(0–255)` · `reverse(0–255)` · `setSpeed(-255..255)` · `stop()` (coast) · `brake()` (active stop)
- **Rules:**
  - Motors on opposite sides of a vehicle face opposite ways, so to drive straight, run one side `forward()` and the other `reverse()`.
  - Never use `analogWrite()` or `digitalWrite()` on the motor pins.
- **Uses:** vehicles, fans, conveyors, robotics.

### Robot Arm Integrator
Drives a six-axis SO-ARM100 or SO-ARM101 arm built from Feetech STS3215 smart servos. The servos daisy-chain on one half-duplex UART bus (1 Mbps), so the whole arm needs one port and one 3-pin cable. Servo power comes from the board's 12V rail through a 3 A fuse, with 3.3V logic. The module extends past the board edge.
- **Requires the Power Jack** for 12V.
- **Can do:** move the arm by hand, record the motion and play it back.
- **Uses:** pick and place, teaching by demonstration, desk robots.

## Storage & Vision

### Micro-SD Card
SPI file storage, gigabytes of space, hot-swappable. Files are readable on a computer afterwards.
- **Pins:** A1 = CS · B1 = SCLK · A2 = MOSI · B2 = MISO
- **API:**
  - Files: `writeFile` · `appendLine` · `readFile` · `readBytes(path,offset,buf,len)` · `readLine(path,i)` · `deleteFile`
  - Directories: `listDir` · `find` (glob) · `grep` · `mkdir` (does not create parents) · `pathJoin` · `pathUp`
  - Card: `cardType` · `cardSizeBytes` · `usedBytes` · `freeBytes` · `remount()` · `isMounted()`
  - Logging: `beginLogger(path)` + `logLine()` (background writer) · `droppedLines()`
- **Rules:**
  - `begin()` returns false when there's no card. That isn't fatal; keep running and offer `remount()`.
  - Only one card per board.
  - It needs a hardware SPI host.
  - Writes block for tens of milliseconds, so for anything recurring, use the logger instead of writing directly.
  - `readFile()` loads the whole file into RAM.
  - Cards must be FAT16/FAT32, not exFAT. Factory-formatted cards of 64 GB and up are usually exFAT and need reformatting.
- **Uses:** data and sensor logging, media, config files.

### Camera (OV5640)
The module has its own ESP32-S3 co-processor that captures and JPEG-encodes frames on board, then sends the compressed frames over SPI. It runs at 3.3V only, with resolutions from 160×120 up to 1280×720.
- **Pins:** A1 = MISO · B1 = MOSI · A2 = SCLK · B2 = CS
- **API:**
  - Capture: `capture()` (blocks ~60–120 ms at VGA) · `frameData()` / `frameLength()` (the JPEG bytes)
  - Streaming: `startStream()` / `stopStream()`. Streaming doesn't push frames to you; it keeps a fresh one ready so `capture()` is faster.
  - Settings: `setResolution(ATECH_CAM_RES_QQVGA/QVGA/VGA/SVGA/XGA/HD)` · `setQuality(0–63, lower = better, default 12)`
  - Status: `statusText()` · `reconnect()` · `retryCount()`
- **Rules:**
  - Trigger captures from a button, a timer or a separate task, never from a tight loop.
  - `"no_sensor"` usually means the ribbon cable is unseated.
  - A climbing `retryCount()` means a marginal connection.
  - It needs a hardware SPI host.
  - Frames can't be sent to the app. Show them on a screen or save them to the SD card.
- **Uses:** photo capture, security monitoring, time-lapse, machine vision.

---

## Combining modules

- **Port budget:** these modules take 2 adjacent ports each: Knob, Screen, Big Screen, Speaker, Microphone, Micro-SD and Camera.
- **SPI hosts:** there are only 2. The Camera and Micro-SD each need one. If you use them together with a screen, call `atech::reserveSpiHosts(n)` first in setup, where n = number of cameras + SD cards; the screen then bit-bangs. Never call `setSpiHost()`.
- **Hard limits:** at most 4 Knobs (one per PCNT unit) and 1 Micro-SD card per board.
- **12V:** the Robot Arm Integrator needs the Power Jack. Motors and servos generally benefit from it.
- **Current-hungry modules:** Light Grid (~540 mA), DC Motor (~500 mA), Camera (~250 mA), Speaker (~200 mA), Knob (~125 mA), Micro-SD (~100 mA). USB-C supplies up to 3 A.
- **Common pairings:**
  - Microphone → Speaker (`writeSamples`) for record and playback.
  - Camera → Screen or SD for viewing and saving frames.
  - Knob or Button → Light Grid or Screen for interactive UIs. Throttle `show()` and redraws.
