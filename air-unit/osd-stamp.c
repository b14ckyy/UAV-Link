/* UAV-Link OSD-Stanzer: vorberechnete Glyphen-Laeufe per memcpy in den
 * Video-Framebuffer schreiben.
 *
 * Warum C statt numpy (Messreihe 15.08., Zero 2 W, 720p60):
 *   - numpy-Fancy-Indexing kostet ~40 ns PRO BYTE (5,6 ms fuer ein OSD) --
 *     reiner Interpreter-/Indexing-Overhead, selbst in gecachtem RAM.
 *   - Die V4L2/DMA-Puffer sind fuer CPU-LESEZUGRIFFE uncached (~125 MB/s);
 *     jede Read-Modify-Write-Idee (Alpha-Mixing!) ist damit tot.
 *   - Binaere Glyphen-Transparenz braucht aber gar kein Lesen: opake
 *     Strecken werden als fertige UYVY-Bytes GESCHRIEBEN, sonst nichts.
 *     Zusammenhaengende Laeufe + memcpy = Store-Buffer-freundlich:
 *     0,83 ms/Frame fuer ein 162-Zellen-OSD, 59+ fps gehalten.
 *
 * runs: flaches uint32-Array aus Tripeln (dst_offset, src_offset, laenge).
 * Die Tabellen baut rtsp-server.py bei jedem DisplayPort-"draw" (~10 Hz)
 * aus den vorkompilierten Font-Fragmenten -- hier laeuft nur der heisse Pfad.
 *
 * Build (macht install.sh): gcc -O3 -shared -fPIC -o libosdstamp.so osd-stamp.c
 */
#include <stdint.h>
#include <string.h>

void stamp(uint8_t *frame, const uint8_t *glyphs,
           const uint32_t *runs, uint32_t n_runs)
{
    for (uint32_t i = 0; i < n_runs; i++) {
        const uint32_t *r = runs + 3 * i;
        memcpy(frame + r[0], glyphs + r[1], r[2]);
    }
}
