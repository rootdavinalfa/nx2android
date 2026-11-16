# Container RomFS and Android UI QOL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional single-file RomFS container (the legacy folder stays the default) whose file location the user can override, plus the Android start screen, ROM-location change, a save folder that can be a Google Drive folder, save export/import to a user folder, and a missing-RomFS notice.

**Architecture:** The container is a new pack format read by the host's existing `zf_*` block reader; the installer pushes it as one resumable file. The UI work is a new start-screen state in the Android front end (drawn by the existing Vulkan menu renderer), new pause-menu rows, and a Java bridge for the folder picker and the SAF copy. Every new path is opt-in: a profile without `romfs container` and a device without `start_screen=0` behave exactly as today.

**Tech Stack:** Python 3.14 (porter), C (host `hle_fs.c`, front end `front_android.c`, renderer `gfx_vk_present.c`), Java (`ExternalActivity.java`), Vulkan overlay, Android SAF.

**Spec:** this document (the user's request). The three parts are independent; ship them in order A, B, C.

## Global Constraints

- The legacy folder RomFS (`RomFS/` with one file per game file, `--romfs-at <folder>`) must keep working unchanged. The container is opt-in per profile and per install.
- No game IDs, names or packages in core code (`tests/arch_rules.py`). Comments may name a game a rule was found with.
- English everywhere; log lines for something unimplemented or wrong start with `[RED] `.
- Ask before installing on the user's device; test builds use `--package org.nx2a.<name>.test` and are uninstalled afterwards.
- Tests: `python -m unittest discover tests` (includes `tests/arch_rules.py`).
- Commit only when the user asks; Conventional Commits.
- The container file location is a fact of one device, not of the pack: it goes in the device's `env.txt` under a marker row, like `DM_ROMFS` today.

## Review Focus

- A `DM_ROMFS` path that is a directory (legacy) vs a file (container): the host must pick the right reader and never treat a folder as a container.
- A truncated or corrupt container: the app must log `[RED] ` and show the start screen's "RomFS not found" notice, not crash.
- A ROM-location change that fails mid-copy: the old location must still be readable, and the app must not write the new path into `env.txt` until the copy is whole.
- A save export to a folder the user cannot write: a clear error, no data loss, the save in internal storage untouched.
- A SAF folder that is offline or revoked: a clear error, no data loss, the local save untouched.
- `start_screen=0`: the game starts exactly as today, with no extra screen and no extra delay.

---

## Part A: Container RomFS (optional)

### Task 1: Container format and packer

**Files:**
- Create: `porter/romfs_container.py`
- Test: `tests/test_romfs_container.py`

**Interfaces:**
- Produces: `MAGIC = b'NXZC'`, `VERSION = 1`, `HDR = struct.Struct('<4sIIQQQ')` (magic, version, entry count, index offset, data offset, reserved), `pack(src: Path, dst: Path, jobs: int = 8, progress=None) -> tuple[int, int]`, `unpack(container: Path, dst: Path) -> int`, `is_container(path: Path) -> bool`, `read_index(container: Path) -> dict[str, tuple[int, int, int, int]]` (path → raw size, block size, block count, block table offset).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_romfs_container.py
import tempfile, unittest
from pathlib import Path
from porter import romfs_container as rc

class ContainerTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst, out = Path(t) / 'src', Path(t) / 'romfs.nxz', Path(t) / 'out'
            (src / 'a').mkdir(parents=True); (src / 'a' / 'b.bin').write_bytes(b'x' * 300_000)
            (src / 'c.txt').write_bytes(b'hello')
            files, size = rc.pack(src, dst)
            self.assertEqual(files, 2); self.assertTrue(rc.is_container(dst))
            self.assertEqual(rc.unpack(dst, out), 2)
            self.assertEqual((out / 'a' / 'b.bin').read_bytes(), b'x' * 300_000)
            self.assertEqual((out / 'c.txt').read_bytes(), b'hello')

    def test_corrupt_header_is_not_a_container(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'bad.nxz'; p.write_bytes(b'NXZC' + b'\0' * 8)
            self.assertFalse(rc.is_container(p))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_romfs_container -v`
Expected: FAIL with `ModuleNotFoundError: porter.romfs_container`

- [ ] **Step 3: Implement `porter/romfs_container.py`**

Header: `'NXZC'`, u32 version 1, u32 entry count, u64 index offset, u64 data offset, u64 0. Index entry: u16 path length, path bytes, u64 raw size, u32 block size, u32 block count, u64 block table offset. Block table: `u64 offsets[n+1]`, then the blocks (zstd or stored), reusing `romfs_pack.pack_stream`'s block rule. `pack` writes the index and data in one pass, one file at a time, with a thread pool for compression; `unpack` reads the index and writes each file. `is_container` checks the magic and version and that the index offset is inside the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_romfs_container -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add porter/romfs_container.py tests/test_romfs_container.py
git commit -m "feat(porter): a single-file RomFS container"
```

### Task 2: Host container reader

**Files:**
- Modify: `nxnative/dm/host/hle_fs.c` (the `zf_*` block, the `mount` function, `ReadFile`/`GetFileSize`/`ReadDirectory`)
- Test: `tests/test_container_host.py` (a Python test that builds a container and runs a small C harness, or a device test if no harness exists)

**Interfaces:**
- Consumes: the format from Task A1.
- Produces: `cf_open(const char *path) -> cfile_t *`, `cf_read(cfile_t *, uint64_t off, uint8_t *dst, size_t n, size_t *got) -> int`, `cf_size(cfile_t *) -> uint64_t`, `cf_readdir(cfile_t *, const char *dir, ...)`, `cf_close(cfile_t *)`; the mount uses the container when `DM_ROMFS` is a regular file whose first 4 bytes are `NXZC`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_container_host.py
# Build a container with porter.romfs_container, then run the host's container reader
# through a tiny C harness (nxnative/dm/host/tests/container_read.c) that prints
# "path size" for every entry and the first 16 bytes of one file.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_container_host -v`
Expected: FAIL (the harness or the reader is missing)

- [ ] **Step 3: Implement the reader in `hle_fs.c`**

Add a `cfile_t` with the index (a hash table of path → entry) and a directory tree built at open. `cf_read` reuses the `zf_block` decompression (extract it into a shared `block_read(fd, off, len, want, out)` helper). In `mount`, `stat` the `DM_ROMFS` path: a directory uses the existing folder reader; a regular file with `NXZC` uses `cf_*`; anything else logs `[RED] ` and fails the mount. `ReadFile`, `GetFileSize` and `ReadDirectory` call the container reader when the mount is a container.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_container_host -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nxnative/dm/host/hle_fs.c nxnative/dm/host/tests/container_read.c tests/test_container_host.py
git commit -m "feat(host): read a single-file RomFS container"
```

### Task 3: Packer and profile integration

**Files:**
- Modify: `porter/pipeline.py` (the `romfs_zstd` block around line 840)
- Modify: `nxnative/dm/android/tools/android_profile.py` (the `romfs` key, line 14 and 122)
- Modify: `porter/datapack.py` (the pack layout and `env.txt` rows)
- Test: `tests/test_porter.py`

**Interfaces:**
- Consumes: `romfs_container.pack`.
- Produces: `android.tsv` `romfs container`; the pack holds `romfs.nxz` at its top level and `env.txt` gets `DM_ROMFS_CONTAINER=<files folder>/romfs.nxz`; `datapack.build` takes `container: Path | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_porter.py
def test_profile_romfs_container_builds_one_file(self):
    # a profile with romfs container: the pack has romfs.nxz, no RomFS/ folder,
    # and env.txt has DM_ROMFS_CONTAINER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_porter -v`
Expected: FAIL (the key is rejected)

- [ ] **Step 3: Implement**

`android_profile.py`: accept `container` in the `romfs` key. `pipeline.py`: when `self.ap.romfs_container`, call `romfs_container.pack(self.game / 'RomFS', self.aw / 'romfs.nxz', self.o.jobs, progress=...)` and pass `container=` to `datapack.build` instead of `romfs=`. `datapack.build`: copy the container to the pack root, add `DM_ROMFS_CONTAINER` to `env.txt`, and count its bytes in the manifest.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_porter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add porter/pipeline.py nxnative/dm/android/tools/android_profile.py porter/datapack.py tests/test_porter.py
git commit -m "feat(profile): romfs container packs the RomFS as one file"
```

### Task 4: Installer and location override

**Files:**
- Modify: `porter/install.py` (the `install` function, the `--romfs-at` handling)
- Modify: `porter/cli.py` (the `_romfs_at` helper)
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: the pack's `romfs.nxz` and `DM_ROMFS_CONTAINER`.
- Produces: `--container-at <path>` (a file on the device) and `--container-push`; the installer pushes the container with `push_file` (64 MB pieces, resumable) and writes `DM_ROMFS_CONTAINER=<path>` in the device's `env.txt` under a marker row. `--romfs-at <folder>` keeps its current behavior.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_install.py
def test_container_at_pushes_one_file_and_writes_env(self):
    # install with --container-at /sdcard/Games/fe.nxz: one push_file, no push_tree of RomFS,
    # env.txt has DM_ROMFS_CONTAINER=/sdcard/Games/fe.nxz
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_install -v`
Expected: FAIL (the option is unknown)

- [ ] **Step 3: Implement**

`cli.py`: add `--container-at` and `--container-push` to `_romfs_at`. `install.py`: when `container_at` is set, validate the path (absolute, `/sdcard/` or `/storage/`, not under another app's `Android/data`), push the pack's `romfs.nxz` with `push_file`, and write `DM_ROMFS_CONTAINER` into the device's `env.txt` under a `# porter install --container-at` marker (carried over by later installs, like `DM_ROMFS`). The legacy `--romfs-at` path is unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_install -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add porter/install.py porter/cli.py tests/test_install.py
git commit -m "feat(install): --container-at puts the RomFS container where the user wants"
```

### Task 5: GUI option

**Files:**
- Modify: `gui/app.py` (the port options and the install options)
- Test: manual (the GUI has no automated tests)

**Interfaces:**
- Consumes: `--container-at` and the profile's `romfs container`.
- Produces: a "Container format" checkbox and a "Container file location" field; the field is passed as `--container-at` on install.

- [ ] **Step 1: Add the checkbox and field**

In `gui/app.py`, add `self.container = QCheckBox('Container format (one file, faster install)')` and `self.container_at = QLineEdit()` with the placeholder `/sdcard/Games/<game>.nxz`. In `romfs_args`, append `--container-at <path>` when the field is non-empty.

- [ ] **Step 2: Manual test**

Run the GUI, port a small game with the checkbox on, install with a container path, and confirm the app starts from the container.

- [ ] **Step 3: Commit**

```bash
git add gui/app.py
git commit -m "feat(gui): container format and its location"
```

---

## Part B: Start screen, ROM location, missing-RomFS notice

### Task 6: Start screen state

**Files:**
- Modify: `nxnative/dm/android/front_android.c` (the `ANativeActivity_onCreate` start, the settings)
- Modify: `nxnative/dm/android/gfx_vk_present.c` (the menu renderer)
- Modify: `nxnative/dm/android/overlay_layout.h` (the start-screen rows)
- Test: device test

**Interfaces:**
- Produces: `g_start_screen` (0 = off, 1 = on), `start_screen_load()` (reads `settings.txt` `start_screen=`), `start_screen_show()`, `start_screen_start()` (starts the host thread); `settings.txt` `start_screen=0` skips the screen.

- [ ] **Step 1: Add the state and the setting**

In `front_android.c`, add `static _Atomic int g_start_screen = 1;` and read `start_screen=%d` in `settings_load`; write it in `settings_save`. In `ANativeActivity_onCreate`, after `romfs_readable`, if `g_start_screen` is on, show the start screen and return without starting the host; the "Start game" row calls `start_screen_start()` (the existing host-thread code moved into a function).

- [ ] **Step 2: Draw the start screen**

In `gfx_vk_present.c`, add a `start_panel` that reuses the menu panel and rows: "ROM data: <path>", "Save: Export / Import / Sync", "Start game", "Don't show this screen again". The checkbox writes `start_screen=0` to `settings.txt`.

- [ ] **Step 3: Device test**

Install a test build, open the app: the start screen shows. Tick "Don't show again", start the game, close and reopen: the game starts directly.

- [ ] **Step 4: Commit**

```bash
git add nxnative/dm/android/front_android.c nxnative/dm/android/gfx_vk_present.c nxnative/dm/android/overlay_layout.h
git commit -m "feat(android): a start screen with a skip option"
```

### Task 7: ROM data location change

**Files:**
- Modify: `nxnative/dm/android/front_android.c` (the start screen and the pause menu)
- Modify: `nxnative/dm/android/java/org/nx2a/ExternalActivity.java` (the folder picker)
- Modify: `nxnative/dm/android/saves_android.c` (reuse `copy_tree` for the move)
- Test: device test

**Interfaces:**
- Produces: `romfs_location_change(const char *new_path)` (copies or moves the RomFS, then writes `DM_ROMFS`/`DM_ROMFS_CONTAINER` to `env.txt`); a Java `pickFolder()` returning a SAF URI or a path.

- [ ] **Step 1: Add the folder picker**

In `ExternalActivity.java`, add `public String pickFolder()` using `ACTION_OPEN_DOCUMENT_TREE` and return the chosen path (or a SAF URI). The C side calls it through JNI.

- [ ] **Step 2: Add the change flow**

In `front_android.c`, the "ROM data location" row opens the picker, then shows an alert: "Changing the location needs a copy/move. This can take a while." On confirm, copy the RomFS to the new path (a container is one file: `rename` when same volume, else copy), verify the size, then write the new path to `env.txt`. On failure, keep the old path and show the error.

- [ ] **Step 3: Device test**

Change the location to `/sdcard/Games/fe`, confirm the copy, restart: the game starts from the new location. Change it back to the app's files folder.

- [ ] **Step 4: Commit**

```bash
git add nxnative/dm/android/front_android.c nxnative/dm/android/java/org/nx2a/ExternalActivity.java nxnative/dm/android/saves_android.c
git commit -m "feat(android): change the RomFS location from the UI"
```

### Task 8: Missing-RomFS notice

**Files:**
- Modify: `nxnative/dm/android/front_android.c` (`romfs_readable`)
- Test: device test

**Interfaces:**
- Consumes: the start screen from Task B1.
- Produces: `romfs_readable` returns 0 and sets a `g_romfs_missing` message instead of ending the app; the start screen shows "RomFS not found: copy/push it first" and a "Retry" button.

- [ ] **Step 1: Change the failure path**

In `romfs_readable`, replace the `g_quit = 1` on a missing folder with `g_romfs_missing = 1` and a `[RED] ` log line. The start screen shows the message and a "Retry" row that re-runs `romfs_readable`.

- [ ] **Step 2: Device test**

Remove the RomFS folder, open the app: the start screen shows the notice. Push the RomFS back, press Retry: the game starts.

- [ ] **Step 3: Commit**

```bash
git add nxnative/dm/android/front_android.c
git commit -m "feat(android): say the RomFS is missing instead of closing"
```

### Task 9: Pause-menu integration and save-first alert

**Files:**
- Modify: `nxnative/dm/android/overlay_layout.h` (the menu rows)
- Modify: `nxnative/dm/android/front_android.c` (the menu actions)
- Modify: `nxnative/dm/android/gfx_vk_present.c` (the alert)
- Test: device test

**Interfaces:**
- Produces: a "Start screen" row in the "Game" tab; before opening it, an alert "Save your game first. The game will be held." with "Save and continue" / "Cancel".

- [ ] **Step 1: Add the row and the alert**

Add `OVM_START` to the menu items and the "Game" tab. In `menu_activate`, when `OVM_START` is chosen, show the alert; on "Save and continue", close the menu, open the start screen, and keep the game held.

- [ ] **Step 2: Device test**

Open the pause menu, choose "Start screen": the alert shows. Cancel: the game resumes. Confirm: the start screen opens, and the game is held.

- [ ] **Step 3: Commit**

```bash
git add nxnative/dm/android/overlay_layout.h nxnative/dm/android/front_android.c nxnative/dm/android/gfx_vk_present.c
git commit -m "feat(android): the pause menu reaches the start screen, after a save reminder"
```

---

## Part C: Save folder (including a Google Drive folder), no OAuth

The save stays in the app's internal storage (`$DM_SAVE`). The user picks a folder that is either a normal path (a folder a sync app such as Google Drive, FolderSync or Syncthing already syncs) or a SAF folder (the Google Drive app's own provider). A normal path becomes `DM_SAVE` directly, so the game reads and writes there; a SAF folder is mirrored on start and exit, because the host can only `open()` normal paths. No OAuth, no Drive API: the Drive app does the network sync.

### Task 10: Save folder setting

**Files:**
- Modify: `nxnative/dm/android/front_android.c` (the setting and the start/exit hooks)
- Modify: `nxnative/dm/android/java/org/nx2a/ExternalActivity.java` (the folder picker and the SAF copy)
- Modify: `nxnative/dm/android/saves_android.c` (the mirror helpers)
- Test: device test

**Interfaces:**
- Produces: `save_folder` in `settings.txt` (a normal path or a `content://` URI); `dm_saves_mirror_out(const char *dest)` and `dm_saves_mirror_in(const char *src)`; `ExternalActivity.pickFolder()` and `ExternalActivity.copyTree(uri, localDir, toUri)`.

- [ ] **Step 1: Add the setting and the picker**

In `front_android.c`, read `save_folder=` in `settings_load` and write it in `settings_save`. Add a "Save folder" row to the start screen and the "Game" tab; it calls `pickFolder()` and stores the result.

- [ ] **Step 2: Use a normal path directly**

If `save_folder` is a normal path (not `content://`), set `DM_SAVE` to it before the host starts (instead of the internal `save` folder). The game then reads and writes the save there; the user's sync app handles Drive.

- [ ] **Step 3: Mirror a SAF folder**

If `save_folder` is a `content://` URI, keep `DM_SAVE` internal and call `dm_saves_mirror_in(save_folder)` before the host starts and `dm_saves_mirror_out(save_folder)` when the app pauses or ends. The Java side copies the tree with `DocumentFile` (create/overwrite each file). A "Sync now" row does the same on demand.

- [ ] **Step 4: Device test**

Pick a normal folder synced by the Drive app: the game writes the save there. Pick the Drive app's SAF folder: the save is mirrored on start/exit. Change the folder back to the app's internal save.

- [ ] **Step 5: Commit**

```bash
git add nxnative/dm/android/front_android.c nxnative/dm/android/java/org/nx2a/ExternalActivity.java nxnative/dm/android/saves_android.c
git commit -m "feat(android): a save folder, including a Google Drive folder, with no OAuth"
```

### Task 11: Export/import to a user folder

**Files:**
- Modify: `nxnative/dm/android/saves_android.c` (a destination path)
- Modify: `nxnative/dm/android/front_android.c` (the menu rows)
- Test: device test

**Interfaces:**
- Produces: `dm_saves_export_to(const char *dest, ...)` and `dm_saves_import_from(const char *src, ...)`; the existing `saves/` export stays.

- [ ] **Step 1: Add the destination path**

In `saves_android.c`, add `dm_saves_export_to(dest, ...)` that copies the save tree to `dest` (a normal folder the user chose) instead of `saves/`. Add `dm_saves_import_from(src, ...)` that copies from `src` into `saves/` and names it in `import.txt`.

- [ ] **Step 2: Add the rows**

In `front_android.c`, add "Export save to folder" and "Import save from folder" to the "Game" tab.

- [ ] **Step 3: Device test**

Export the save to `/sdcard/Download/fe-save`, delete the app's save, import from that folder, start the game: the save is back.

- [ ] **Step 4: Commit**

```bash
git add nxnative/dm/android/saves_android.c nxnative/dm/android/front_android.c
git commit -m "feat(android): export and import a save to a folder the user picks"
```

### Task 12: Start-screen and overlay integration

**Files:**
- Modify: `nxnative/dm/android/front_android.c` (the start screen rows)
- Modify: `nxnative/dm/android/overlay_layout.h` (the start-screen rows)
- Test: device test

**Interfaces:**
- Consumes: Tasks 6, 10, 11.
- Produces: the start screen shows "ROM data", "Save folder", "Export save", "Import save", "Sync now", "Start game", "Don't show again"; the pause menu shows the same save rows.

- [ ] **Step 1: Add the rows**

Add the save rows to the start screen and the "Game" tab, reusing the actions from C1 and C2.

- [ ] **Step 2: Device test**

Open the start screen: all rows are there. Export, import, sync from the start screen; then from the pause menu.

- [ ] **Step 3: Commit**

```bash
git add nxnative/dm/android/front_android.c nxnative/dm/android/overlay_layout.h
git commit -m "feat(android): the start screen and the pause menu share the save tools"
```

---

## Self-Review

- **Spec coverage:** container (A1–A5), location override (A4, B2), start screen and skip (B1), ROM location change with alert (B2), missing-RomFS notice (B3), save folder including a Google Drive folder (C1), export/import to a folder (C2), overlay integration and save-first alert (B4, C3). All covered.
- **Legacy:** A4 keeps `--romfs-at <folder>`; B1 keeps the direct start when `start_screen=0`; C1 keeps the internal save when no folder is set; C2 keeps the existing `saves/` export.
- **Type consistency:** `romfs_container.pack`/`unpack`/`is_container` are used by A2–A4; `dm_saves_mirror_out`/`dm_saves_mirror_in` are used by C1 and C3; `dm_saves_export_to`/`dm_saves_import_from` are used by C2 and C3; `pickFolder()` is shared by B2 and C1.
- **Review focus:** each line has a task: A2 (directory vs file, corrupt container), B2 (failed move), C1 (SAF folder offline or revoked), C2 (unwritable folder), B1 (`start_screen=0`).
