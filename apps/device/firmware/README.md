# Firmware handoff — hardware owner

This is an integration specification, **not compiled firmware**. The exact board,
modules, and physical ports must be confirmed before generating or flashing code.
No board has been flashed by this foundation.

1. Inventory the board and modules with the Atech team. Record real port assignments
   in `docs/hardware.md`.
2. Install Atech in a virtual environment, run `atech guide`, then use the SDK's
   workflow to generate a project for the confirmed board and modules. The first
   build may download a large toolchain; prepare before the demo.
3. Generate firmware that emits one JSON object plus newline on each **press edge**
   (not continuously while held):

   ```json
   {"event_type":"button","key":"kitchen","value":true}
   ```

   For the other two buttons use `bathroom` and `printer`. If generated firmware
   instead uses keys such as `button_1`, pass a JSON mapping via `--mapping`.
   The key name does not imply any physical port number.

4. A calibrated low-stock detector should emit once when transitioning into the
   low state, and re-arm only after replenishment (use hysteresis/debouncing):

   ```json
   {"event_type":"stock_low","key":"bathroom","value":{"item_id":"toilet-paper","level":15}}
   ```

   `level` is a **calibrated percentage**, not a raw distance reading. Distance-to-
   stock calibration depends on the physical holder. Generic raw sensor telemetry
   is ignored by the bridge. Atech's USB wrapper is also accepted:

   ```json
   {"type":"event","payload":{"event_type":"button","key":"kitchen","value":true}}
   ```

5. Add the custom `office_feedback` action handler to the firmware. It receives:

   ```json
   {"action":"office_feedback","value":{"station_id":"kitchen","text":"Awaiting input","color":"blue"}}
   ```

   Map text to the installed display and colour to the installed LED module.
   This is an application-specific action, **not a built-in Atech capability**.

6. Build, validate, upload, and check using the installed SDK's instructions. Test
   all actual modules before saying hardware works.

The bridge sends immediate API response feedback and polls the latest command
for every configured station about once per second. Dashboard approvals and
resolutions therefore also update the board. Identical commands are suppressed;
a null command leaves the current display unchanged. This is latest-state polling,
not a guaranteed queue of all transitions. For deployment beyond this local demo,
add device authentication with the backend owner.

Reference: https://atech.dev/docs

## Enclosure reference

The team's supplied [Atech Atelier repository](https://github.com/atechdotdev/atech-atelier)
is a CAD/design tool with board and module models. Use it to design the station
case; use the current Atech SDK and actual module inventory for firmware.
Neither CAD model placement nor an example project establishes the physical
port assignments on our board. See `docs/hardware.md` for the source distinction.
