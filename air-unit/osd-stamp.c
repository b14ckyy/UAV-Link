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
 *   - Weiche Kanten gehen trotzdem: blend() mischt NUR die Saumbytes
 *     (0 < Alpha < voll nach dem Skalieren) ins Bild. Das ist der einzige
 *     Ort mit uncached-Reads, aber der Saum ist klein gegen die Flaeche --
 *     die Innenflaechen bleiben reine memcpy-Laeufe ohne Lesezugriff.
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

/* Saumbytes: dst = dst + ((src - dst) * alpha + 128) / 256. Alpha 0 ist
 * damit EXAKT Identitaet -- wichtig, weil die Tabellen Luecken zwischen
 * Saumstuecken mit auffuellen (s. u.). val und alpha sind parallele
 * Pools, die src_offset gemeinsam indiziert.
 *
 * Der Framebuffer ist fuer CPU-Reads uncached und jede Transaktion
 * kostet ~500 ns Latenz -- gemessen 15.08.: 8400 kurze Saumlaeufe
 * einzeln gelesen = 4,8 ms/Frame, egal ob byte- oder blockweise.
 * Deshalb baut rtsp-server.py MERGED Laeufe (Luecken <= 24 B aufgefuellt,
 * Fuellbytes blenden per Alpha zu sich selbst): wenige lange Reads statt
 * vieler kurzer. Hier: Lauf per memcpy in den Stack-Puffer (breite
 * Loads), gecacht mischen, zurueckschreiben. */
void blend(uint8_t *frame, const uint8_t *val, const uint8_t *alpha,
           const uint32_t *runs, uint32_t n_runs)
{
    uint8_t buf[512];
    for (uint32_t i = 0; i < n_runs; i++) {
        const uint32_t *r = runs + 3 * i;
        uint32_t off = r[0], so = r[1], left = r[2];
        while (left) {                          /* Laeufe zeilenbegrenzt */
            uint32_t n = left > sizeof buf ? sizeof buf : left;
            memcpy(buf, frame + off, n);
            for (uint32_t j = 0; j < n; j++) {
                int32_t d = buf[j];
                buf[j] = (uint8_t)(d + ((((int32_t)val[so + j] - d)
                                         * alpha[so + j] + 128) >> 8));
            }
            memcpy(frame + off, buf, n);
            off += n; so += n; left -= n;
        }
    }
}
