// The save manager (saves_android.c): exports of the game's save in the app's external files folder (saves/), and imports from there at the next start
#pragma once
#include <stdint.h>
#include <time.h>

typedef struct { char name[64]; long files; long long bytes; time_t mtime; int device; } dm_save_entry;   // an export or a save folder put there
void dm_saves_paths(const char *files_root);   // saves/ under the files folder, $DM_SAVE (dm_saves_start calls it)
const char *dm_saves_dir(void);
// at the start, before the host: a pending import (saves/import.txt), then a pending export (saves/export.txt). Returns 1 when the request asks the app to end
int dm_saves_start(const char *files_root);
// the committed save (and the device save) to saves/<YYYYMMDD-HHMMSS><suffix>: 0 and its name, or -errno and why (-ENOENT: the game has not saved yet)
int dm_saves_export(const char *suffix, char *name, int cap, char *err, int ecap);
int dm_saves_export_to(const char *dest, char *name, int cap, char *err, int ecap);   // the save to a folder the player picked (a timestamped subfolder)
int dm_saves_import_from(const char *src, char *name, int cap, char *err, int ecap);  // a folder the player picked into saves/, imported at the next start
int dm_saves_list(dm_save_entry *out, int max);   // the save folders in saves/, newest first
int dm_saves_request_import(const char *name);    // name is imported at the next start: 0, or -1 when the request cannot be written
// src's tree into dst (made here: it must not exist); *files and *bytes add up what was copied; the first failure's -errno, its path in err
int dm_copy_tree(const char *src, const char *dst, long *files, long long *bytes, char *err, int ecap);
