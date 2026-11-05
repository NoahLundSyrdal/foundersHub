
# Atech

> Modular electronics kit: pre-built modules slide into an ESP32-S3 board, no soldering. Build from atech.dev or from your own coding agent with the Python SDK (pip install atech).

## For AI agents

If you were asked to learn about Atech, this file is the complete reference. Read it all, then tell the user what Atech is (Overview below) and what they can build with the modules.

To build or program real Atech hardware, install the open-source Python SDK on the user's machine and read its guide before writing anything:

    pip install atech        (or: uv pip install atech; needs Python 3.10+)
    atech guide              the SDK's manual for coding agents: workflow, rules, pitfalls
    atech list-modules       every module, with its API

Ask the user which board and modules they own, and which port each module is in, before writing firmware. The full workflow is under "Build with your own agent" at the end of this file. If they have no board yet, point them to https://atech.dev/buy.

More:
- MCP connector for Claude, ChatGPT and other assistants: https://atech.dev/mcp
- SDK source and README: https://github.com/atechdotdev/python-sdk
- SDK on PyPI: https://pypi.org/project/atech/
- Docs for people: https://atech.dev/docs
- Help: https://discord.gg/B6kuhUW27S, hello@atech.dev

## Overview

Atech (atech.dev) is a modular electronics kit from Copenhagen: pre-built modules slide into numbered ports on an ESP32-S3 motherboard, no soldering. You describe what to build at https://atech.dev/editor, the AI picks modules and writes the firmware, and you upload it from the browser.

Board: 12 module ports (1-7, 9-11, 13-14). Port 8 is the USB-C socket, port 12 the reset button. Double-width modules take two neighbouring ports on one side: 1-2, 2-3, 3-4, 4-5, 5-6, 9-10, 10-11, 13-14. Moving a module means moving it in the editor too and uploading again.

Needs: a desktop Chromium browser (Chrome, Edge, Brave, Opera, Arc; uploading uses Web Serial, which Firefox, Safari and phones lack) and a USB-C data cable (charge-only cables cannot program the board).

Steps: 1) describe the build at https://atech.dev/editor, 2) put the modules where the diagram shows, 3) plug in over USB-C (no driver), 4) pick WiFi (add WiFi credentials in the connection pill; 2.4 GHz only) or USB (click "Connect device via USB" once), 5) click "Upload Code".

Protocol: the board speaks JSON. Events come from the board: {"event_type":"sensor|button|state|log","key":"<event key>","value":<value>}. Actions go to it: {"action":"<action key>","value":<value>}. Over WiFi: WebSocket wss://gateway.atech.dev/ws/live/<project id>; the board's messages arrive as {"type":"device_event","payload":{...}}, you send {"type":"send_to_device","device_id":"<project id>","payload":{...}}. Over USB: 115200 baud, one JSON object per line; from the board {"type":"event","payload":{...}}, to the board {"action":...,"value":...}. The project id is in the editor's Docs tab under "Your project".

Modules: Color Display (st7735_tft, 2 ports), LED Grid (neopixel_grid), Orientation Sensor (icm40608_imu), Temperature & Humidity (aht20_sensor), Air Quality (scd40_air_quality), Distance Sensor (vl53l5cx_distance), Knob (rotary_encoder_knob, 2 ports), Button (button_module), Speaker (speaker, 2 ports), Microphone (microphone, 2 ports), DC Motor (dc_motor), Big Screen (st7789_tft, 2 ports), Soil Sensor (soil_sensor), Camera (ov5640_camera, 2 ports), microSD Card (micro_sd_card, 2 ports).

Own agent: pip install atech (Python SDK, open source, Python 3.10+) gives Claude Code, Codex, Cursor or any coding agent the module catalogue, each module's API and an offline compiler. The agent runs atech guide first, then atech list-modules, atech new, atech validate, atech build, atech upload, atech check. The first build downloads the ESP32 compiler (about 2.4 GB, once).

Troubleshooting: board will not connect or upload fails -> unplug and replug, close other atech.dev tabs and the Arduino Serial Monitor (one program can hold the port), use a data cable, no hub, press reset, on Linux join the dialout group. No upload button -> not a desktop Chromium browser. Board offline after upload -> wait up to 60 s, then replug or reset; on WiFi check the credentials. Code connects but nothing arrives -> board is not online, check "Receiving frames" in the connection pill. Connection refused -> wrong project id in the address. Action ignored -> the firmware only accepts actions of the modules in that build.

Help: Discord https://discord.gg/B6kuhUW27S, hello@atech.dev. Full reference with every module: https://atech.dev/llms.txt. Connector for Claude, ChatGPT and other assistants (MCP): https://atech.dev/mcp

## Module reference

### Color Display (st7735_tft) - takes 2 neighbouring ports
part: ST7735 TFT Color Display (160x80); interface: Digital; chip: ST7735S (Sitronix); current: ~25 mA; list price: $19.00 USD
Use it for: Shows words, pictures and simple games on a small color screen.
What it can do:
- display_text: Display text at cursor position with configurable size and color
- clear_display: Clear the display to black
- fill_screen: Fill entire screen with a color
- draw_pixel: Draw a single pixel at x,y with color
- draw_line: Draw a line between two points
- draw_rect: Draw a rectangle outline
- fill_rect: Draw a filled rectangle
- draw_circle: Draw a circle outline
- fill_circle: Draw a filled circle
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = DC, data/command select; Data B (first port) = CS, chip select; 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = MOSI, data in; Data B (second port) = SCLK, serial clock

### LED Grid (neopixel_grid) - takes 1 port
part: NeoPixel 3x3 LED Grid; interface: Digital; chip: WS2812B (Worldsemi); current: ~540 mA; list price: $12.00 USD
Use it for: A small square of nine colored lights for patterns, signals and glowing effects.
What it can do:
- set_pixel: Set a single LED by index (0-8) to RGB color
- set_all: Set all LEDs to the same RGB color
- set_row: Set an entire row to a color
- set_column: Set an entire column to a color
- clear: Turn off all LEDs
- show: Update physical LEDs with current colors
- set_brightness: Set global brightness (0-255). Values above 51 are clamped to 51 (20% cap).
- get_pixel_color: Get a pixel's color as packed uint32_t (0x00RRGGBB). Extract: r = (color >> 16) & 0xFF, g = (color >> 8) & 0xFF, b = color & 0xFF
Pins: 3.3V = supply; GND = ground; Data A = LED data (V1.x, RGB); Data B = LED data (V2.x, RGBW)

### Orientation Sensor (icm40608_imu) - takes 1 port
part: 6-Axis IMU (ICM-40608 or LSM6DSOX); interface: I2C (0x68); chip: LSM6DSOX (STMicroelectronics); current: ~4 mA; list price: $19.00 USD
Use it for: Feels tilting, shaking and turning, so your project can react to how it is moved.
What it can do:
- read_accel_x (returns float, g): Read X-axis acceleration in g
- read_accel_y (returns float, g): Read Y-axis acceleration in g
- read_accel_z (returns float, g): Read Z-axis acceleration in g
- read_gyro_x (returns float, deg/s): Read X-axis rotation rate in degrees/second
- read_gyro_y (returns float, deg/s): Read Y-axis rotation rate in degrees/second
- read_gyro_z (returns float, deg/s): Read Z-axis rotation rate in degrees/second
- read_temperature (returns float, C): Read sensor die temperature
- get_pitch (returns float, deg): Pitch angle (nose up/down) in degrees, -90..+90, from accelerometer
- get_roll (returns float, deg): Roll angle (left/right tilt) in degrees, -180..+180, from accelerometer
- get_tilt_angle (returns float, deg): Total tilt from upright in degrees (0 = flat face-up, 180 = flat face-down)
- get_orientation (returns string): Coarse 6-way orientation: face_up, face_down, edge_right, edge_left, edge_front, edge_back, unknown
- was_shaken (returns bool): Edge-triggered shake/tap detector (returns true once per spike >1.8g, then resets)
Pins: 3.3V = supply; GND = ground; Data A = I²C SDA; Data B = I²C SCL

### Temperature & Humidity (aht20_sensor) - takes 1 port
part: AHT20 Temperature & Humidity Sensor; interface: I2C (0x38); chip: AHT20 (ASAIR (Guangzhou Aosong Electronics)); current: ~1 mA; list price: $19.00 USD
Use it for: Measures how warm and how damp the air around your project is.
What it can do:
- read_temperature (returns float, C): Read temperature in Celsius
- read_humidity (returns float, %RH): Read relative humidity
Pins: 3.3V = supply; GND = ground; Data A = I²C SDA; Data B = I²C SCL

### Air Quality (scd40_air_quality) - takes 1 port
part: SCD40 Air Quality Sensor (CO2); interface: I2C (0x62); chip: SCD40 (Sensirion); current: ~18 mA; list price: $49.00 USD
Use it for: Tells you how fresh the air in a room is, so you know when to let some air in.
What it can do:
- read_co2 (returns int, ppm): Read CO2 concentration in ppm
- read_temperature (returns float, C): Read temperature in Celsius
- read_humidity (returns float, %RH): Read relative humidity
- is_ready (returns bool): Check if first measurement has been received
Pins: 3.3V = supply; GND = ground; Data A = I²C SDA; Data B = I²C SCL

### Distance Sensor (vl53l5cx_distance) - takes 1 port
part: VL53L5CX 8x8 Distance Sensor; interface: I2C (0x29); chip: VL53L5CX (STMicroelectronics); current: ~20 mA; list price: $19.00 USD
Use it for: Sees how far away things are, so your project can notice people or objects coming close.
What it can do:
- get_distance (returns int, mm): Get distance for a specific zone (0-63) in mm
- get_min_distance (returns int, mm): Get minimum distance across all 64 zones in mm
- get_max_distance (returns int, mm): Get maximum distance across all 64 zones in mm
- is_ready (returns bool): Check if sensor has data available
Pins: 3.3V = supply; GND = ground; Data A = I²C SDA; Data B = I²C SCL

### Knob (rotary_encoder_knob) - takes 2 neighbouring ports
part: Rotary Encoder Knob; interface: Digital; chip: EC11 (generic (Bourns/ALPS-compatible)); current: ~125 mA; list price: $19.00 USD
Use it for: A dial you turn and press to move through menus or adjust things like volume, with a ring of lights that follows it.
What it can do:
- getPosition (returns int, steps): Get cumulative rotation position as int32_t (increments/decrements with each step)
- setPosition (returns void): Set position to a specific value: setPosition(int32_t pos)
- resetPosition (returns void): Reset position counter to zero
- getDirection (returns int): Get direction of last rotation: 1=CW, -1=CCW, 0=none
- wasRotatedCW (returns bool): Check if knob was rotated clockwise since last check
- wasRotatedCCW (returns bool): Check if knob was rotated counter-clockwise since last check
- isPressed (returns bool): Check if knob button is currently pressed (press down on knob)
- wasPressed (returns bool): Check if knob button was just pressed (edge detection)
- update (returns void): Sample encoder, poll button, and refresh the ring - MUST call once per loop iteration. Encoder counts in hardware (PCNT); this call reads the count and repaints the ring.
- setAcceleration (returns void): Configure acceleration: setAcceleration(bool enabled, int maxMultiplier=5). Enabled by default. Slow turns = 1 step, fast spins = up to maxMultiplier steps per click.
- enableRing (returns void): Enable or disable the LED indicator ring: enableRing(bool enabled). Ring is enabled by default.
- setRingColor (returns void): Set ring indicator color: setRingColor(uint8_t r, uint8_t g, uint8_t b). Default is white (255,255,255).
- setRingBrightness (returns void): Set ring global brightness: setRingBrightness(uint8_t brightness). 0-255, default ~50.
- setRingPosition (returns void): Manually override ring position: setRingPosition(float pos). 0.0-12.0 maps around the physical ring (CCW from 12 o'clock). Pass negative to re-couple to knob position.
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = encoder CLK (channel A); Data B (first port) = encoder DT (channel B); 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = button switch, active low; Data B (second port) = NeoPixel ring data

### Button (button_module) - takes 1 port
part: Button; interface: Digital; current: ~20 mA; list price: $12.00 USD
Use it for: A button you press to start, stop or pick things.
What it can do:
- is_pressed (returns bool): Check if button is currently pressed
- was_pressed (returns bool): Check if button was just pressed (edge detection)
- was_released (returns bool): Check if button was just released (edge detection)
Pins: 3.3V = supply; GND = ground; Data A = switch, active low (pressed = LOW); Data B = RGB indicator LED (V2.0 only)

### Speaker (speaker) - takes 2 neighbouring ports
part: I2S Speaker (MAX98357A); interface: Digital; chip: MAX98357A (Analog Devices (Maxim)); current: ~200 mA; list price: $25.00 USD
Use it for: Plays beeps, tunes and other sounds.
What it can do:
- play_tone: Play a tone at given frequency for a duration (blocking)
- play_note: Play a musical note with automatic gap between notes
- play_rtttl: Play an RTTTL melody string (non-blocking, plays in background)
- rest: Play silence for a duration
- stop: Stop any sound immediately
- set_volume: Set volume level (0.0 to 1.0)
- write_samples: Write raw 16-bit mono audio samples directly to I2S output (for streaming from microphone)
- play_pcm: Play a base64-encoded chunk of 16-bit mono PCM streamed from the host. The browser/app decodes the MP3, resamples to 16 kHz mono, and sends play_pcm chunks. The firmware just forwards each chunk to the I2S DAC. This is how the device plays MP3/WAV/audio files, with no on-device decoder needed.
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = LRCLK, left/right clock; Data B (first port) = BCLK, bit clock; 3.3V (second port) = supply; GND (second port) = ground; Data B (second port) = DIN, audio data

### Microphone (microphone) - takes 2 neighbouring ports
part: I2S MEMS Microphone (ICS-43434); interface: Digital; chip: ICS-43434 (TDK InvenSense); current: ~5 mA; list price: $25.00 USD
Use it for: Listens for sound, so your project can react to claps, voices or how loud a room is.
What it can do:
- get_sound_level: Get current RMS sound level (0-32767)
- is_sound_detected: Check if sound exceeds a threshold
- get_peak_amplitude: Get peak amplitude since last read (resets after read)
- get_samples: Copy raw audio samples into a buffer
- start_recording: Start recording audio into heap buffer (up to 10 seconds)
- stop_recording: Stop recording audio
- is_recording: Check if currently recording
- get_recording: Get pointer to recorded audio buffer for playback
- get_recording_length: Get number of recorded samples
- set_channel: Select left or right channel (LR pin)
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = LR, left/right channel select; Data B (first port) = WS, word select; 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = SCK, serial clock; Data B (second port) = SD, serial data

### DC Motor (dc_motor) - takes 1 port
part: DC Motor (H-Bridge); interface: Digital; chip: DRV8833 (Texas Instruments); current: ~500 mA; list price: $19.00 USD
Use it for: Spins wheels, fans or anything else that needs to turn, at the speed you choose.
What it can do:
- forward: Run motor forward at specified speed (0-255)
- reverse: Run motor in reverse at specified speed (0-255)
- stop: Stop the motor (coast)
- brake: Brake the motor (active stop)
- set_speed: Set motor speed and direction (-255 to +255)
Pins: 3.3V = supply; GND = ground; Data A = IN1; Data B = IN2

### Big Screen (st7789_tft) - takes 2 neighbouring ports
part: ST7789 TFT Color Display (320x240); interface: Digital; current: ~40 mA; list price: $25.00 USD
Use it for: Shows dashboards, menus, pictures and games on a bigger color screen.
What it can do:
- display_text: Display text at cursor position with configurable size and color
- clear_display: Clear the display to black
- fill_screen: Fill entire screen with a color
- draw_pixel: Draw a single pixel at x,y with color
- draw_line: Draw a line between two points
- draw_rect: Draw a rectangle outline
- fill_rect: Draw a filled rectangle
- draw_circle: Draw a circle outline
- fill_circle: Draw a filled circle
- draw_round_rect: Draw a rounded rectangle, outline or filled
- draw_triangle: Draw a triangle, outline or filled
- draw_ellipse: Draw an ellipse, outline or filled
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = DC, data/command select (the panel's RS pin); Data B (first port) = CS, chip select; 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = MOSI, data in; Data B (second port) = SCLK, serial clock

### Soil Sensor (soil_sensor) - takes 1 port
part: Soil Moisture Sensor (two-prong galvanic probe); interface: I2C (0x37); chip: CH32V003; current: ~25 mA; list price: $19.00 USD
Use it for: Tells you how wet the soil in a plant pot is, so you know when to water.
What it can do:
- read_moisture (returns float, %): Read calibrated soil moisture, 0-100 %
- read_conductance (returns float, uS): Read conductance between the prongs in microsiemens
- read_raw (returns int, counts): Read the raw oversampled reading (HIGH means DRY)
- calibrate_dry: Record the current reading as the 0 % point; call with the probe in air
- calibrate_wet: Record the current reading as the 100 % point; call with the prongs in water
- set_status_color: Drive the on-module status LED
- set_status_off: Turn the on-module status LED off
Pins: 3.3V = supply; GND = ground; Data A = I²C SDA; Data B = I²C SCL

### Camera (ov5640_camera) - takes 2 neighbouring ports
part: OV5640 Camera (SPI); interface: Digital; current: ~250 mA; list price: $49.00 USD
Use it for: Takes photos you can show on a screen or save to a memory card.
What it can do:
- capture_frame: Capture one JPEG frame into the driver's buffer
- stream: Start or stop continuous capture, so frames are always fresh
- set_resolution: Set capture resolution, QQVGA through HD
- set_quality: Set JPEG quality, 0-63, lower is better
- camera_status: Report link and sensor state
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = MISO: module to board; Data B (first port) = MOSI: board to module; 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = SCLK: serial clock; Data B (second port) = CS: chip select

### microSD Card (micro_sd_card) - takes 2 neighbouring ports
part: microSD Card (SPI); interface: Digital; current: ~100 mA; list price: $12.00 USD
Use it for: Saves logs, pictures and sounds on a memory card you can read on a computer.
What it can do:
- write_file: Write text or bytes to a path, replacing the file
- append_line: Append a line to a file
- read_file: Read a file back
- list_dir: List a directory
- find: Search for files by glob
- grep: Search inside a file for text
- make_dir: Create a directory
- delete_file: Delete a file
- card_info: Card type, capacity, used and free space
- remount: Mount a card that was inserted after boot
- log: Queue lines to a file from a background writer, without blocking
Pins: 3.3V (first port) = supply; GND (first port) = ground; Data A (first port) = CS, chip select; Data B (first port) = SCLK, serial clock; 3.3V (second port) = supply; GND (second port) = ground; Data A (second port) = MOSI; Data B (second port) = MISO

## Build with your own agent

Install: pip install atech or uv pip install atech (Python 3.10+). SDK on PyPI: https://pypi.org/project/atech/, source: https://github.com/atechdotdev/python-sdk
- atech guide: The SDK's manual for coding agents. Read first.
- atech list-modules: Every module in the catalog, with its API.
- atech new <name> --board <id>: A project.yaml, plus AGENTS.md and CLAUDE.md for the agent.
- atech validate: Check the project before building.
- atech build: Compile the project to ESP32 firmware, offline.
- atech upload: Flash the board over USB-C.
- atech check: Reboot the board and see which modules answered.

Prompt to give the agent:

Install the Atech Python SDK and use it to build my project:

    pip install atech

It needs Python 3.10 or newer. Then run `atech guide` and read all of it before
anything else: it is the SDK's own manual for coding agents (the workflow, the
rules, what will bite you). `atech guide protocol` covers the wire format.

Atech is a modular electronics platform: pre-built hardware modules snap into
numbered ports on an ESP32 motherboard, with no soldering or wiring. The SDK
ships the module catalog, each module's API, and a bundled compiler that builds
and flashes the board offline.

Before you write any code, ask me:

1. Which Atech board do I have? Run `atech list-boards` to see the boards and
   their ports.
2. Which modules do I own? Run `atech list-modules` to see every module the
   SDK knows about, and ask me which of those I actually have in hand.
3. Which port is each module plugged into?

If I don't have a board yet, or I'm missing a module the build needs, point me
to https://atech.dev/buy and suggest which kit fits what I described.

Then:

- Run `atech new <name> --board <board id>` and describe the build in its
  project.yaml, using only modules I confirmed I own and the ports I confirmed
  they're in. `atech info <module>` shows a module's API.
- Run `atech validate`, then `atech build`, and fix anything they report.
  Tell me first that the first build downloads the ESP32 compiler, about
  2.4 GB, once.
- Run `atech upload` with my board on USB-C (a data cable), then `atech check`
  to see which modules answered, and tell me what to expect it to do.
