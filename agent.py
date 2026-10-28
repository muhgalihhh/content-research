import os, re, json, glob, time, datetime, httpx
from google import genai
from google.genai import errors, types
from sources import fetch_rss, fetch_github_trending, fetch_article

# urutan fallback kalau model di depannya overload (503) / kena kuota (429). Semua ada di free tier;
# "gemini-4-argon" lebih baru tapi butuh billing. Cek: https://ai.google.dev/gemini-api/docs/models
MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
RETRYABLE_CODES = {429, 500, 503, 504}
REQUEST_TIMEOUT_S = 180  # tahap tulis (maks ~14 ide x 5-7 slide) normalnya < 2 menit
# target jumlah ide per hari (min, max) per pillar bersumber; Tech & Tutorial pillar utama -> jatah terbanyak
IDEAS_PER_PILLAR = {"Tech & Tutorial": (3, 5), "Design & UI/UX": (1, 3), "IT & Tech Industry": (1, 3)}
HISTORY_DAYS = 7  # hook N hari terakhir dikirim ke Gemini supaya topiknya nggak diulang-ulang

# format konten -> keterangan kegunaannya (ditampilin di samping judul tiap ide)
FORMATS = {
    "Feed": "carousel evergreen, tersimpan di grid & gampang ditemukan lewat search/profil",
    "Story": "konten cepat & spontan, cocok quick tips/teaser, otomatis hilang dalam 24 jam",
}

# pillar -> (deskripsi, tujuan, ada data sumber asli?)
PILLARS = {
    "Tech & Tutorial": ("Tips & trik praktis buat developer sehari-hari, rekomendasi tools/website & AI assistant kekinian yang beneran berguna, produktivitas & soft skill kerja dev. Bahasa gampang dicerna, bukan deep-dive kode yang cuma dimengerti senior dev. Mini-tutorial framework (Laravel, Next.js, FastAPI) boleh sesekali tapi bukan mayoritas.", "Bangun kredibilitas teknis", True),
    "Design & UI/UX": ("Proses desain, breakdown UI, tips Figma, before/after.", "Tunjukin skill desain", True),
    "Case Study & Portfolio": ("Showcase project, hasil kerja, cerita di balik project.", "Konversi ke klien", False),
    "IT & Tech Industry": ("Berita/tren industri IT non-teknis -- bisnis software, AI di dunia kerja, produk/startup (termasuk ekosistem tech Indonesia), arah industri.", "Relevansi & insight industri", True),
    "Hari Besar": ("Konten menyambut hari besar/perayaan nasional atau internasional yang dekat tanggal hari ini.", "Merayakan Hari Besar", False),
}
PILLAR_NAMES = list(PILLARS.keys())
GROUNDED_PILLARS = [name for name, meta in PILLARS.items() if meta[2]]
PLACEHOLDER_PILLARS = {name for name, meta in PILLARS.items() if not meta[2]}

# MM-DD -> nama. Tanggal pasti dari kode, bukan dari ingatan Gemini (dulu sering meleset/kelewat).
# Hari besar kalender lunar (Idulfitri, Iduladha, Imlek, Nyepi, Waisak, dll.) geser tiap tahun: tambah manual per tahun.
HARI_BESAR = {
    "01-01": "Tahun Baru Masehi",
    "02-09": "Hari Pers Nasional",
    "03-08": "Hari Perempuan Internasional",
    "04-21": "Hari Kartini",
    "05-02": "Hari Pendidikan Nasional",
    "05-20": "Hari Kebangkitan Nasional",
    "06-01": "Hari Lahir Pancasila",
    "08-17": "HUT Kemerdekaan RI",
    "09-13": "Hari Programmer",  # hari ke-256; tahun kabisat jatuh 09-12
    "10-01": "Hari Kesaktian Pancasila",
    "10-02": "Hari Batik Nasional",
    "10-10": "Hari Kesehatan Mental Sedunia",
    "10-28": "Hari Sumpah Pemuda",
    "11-10": "Hari Pahlawan",
    "11-25": "Hari Guru Nasional",
    "12-22": "Hari Ibu",
    "12-25": "Hari Natal",
}
HARI_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
            "Agustus", "September", "Oktober", "November", "Desember"]

REQUIRED_FIELDS = ("pillar", "format", "hook", "angle", "outline", "source_id")

SELECT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "pillar": {"type": "string", "enum": GROUNDED_PILLARS},
            "why": {"type": "string"},
        },
        "required": ["id", "pillar", "why"],
    },
}

WRITE_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "pillar": {"type": "string", "enum": PILLAR_NAMES},
            "format": {"type": "string", "enum": list(FORMATS.keys())},
            "hook": {"type": "string"},
            "angle": {"type": "string"},
            "outline": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slide_title": {"type": "string"},
                        "copy": {"type": "string"},
                    },
                    "required": ["slide_title", "copy"],
                },
            },
            "source_id": {"type": "integer"},
        },
        "required": list(REQUIRED_FIELDS),
    },
}

SELECT_PROMPT = """Kamu editor konten Instagram brand xharp.dev (software development & design studio).
Audiens: developer, desainer, & orang yang tertarik dunia IT di Indonesia.
Tanggal hari ini: {today}.

Dari daftar sumber di bawah, pilih yang PALING layak dijadikan konten hari ini untuk pillar berikut:
{pillar_block}

Kriteria (urut prioritas):
1. Bermanfaat & relevan buat audiens di atas -- bukan berita receh, clickbait, atau drama.
2. Ada alasan kenapa dibahas SEKARANG: rilis baru, update besar, tren, kejadian minggu ini.
3. Topik yang muncul di beberapa sumber sekaligus berarti lagi hangat -- prioritaskan.
4. JANGAN pilih topik yang sama/mirip dengan konten {history_days} hari terakhir ini:
{recent_hooks}

Jumlah sumber per pillar ikuti angka "pilih X-Y" di tiap pillar. Boleh di bawah minimum HANYA kalau memang
nggak ada kandidat yang layak -- jangan pilih sumber jelek demi memenuhi jumlah. Di pillar yang slotnya banyak,
variasikan sub-topiknya (mis. tools/AI assistant, tips produktivitas, tutorial singkat), jangan semua sejenis.
Satu sumber hanya boleh dipilih sekali. Isi "why" dengan 1 kalimat alasan memilih.
Balas HANYA JSON array sesuai schema. Daftar sumber (pakai "id"-nya):
{sources}"""

WRITE_PROMPT = """Kamu content strategist buat brand xharp.dev (software development & design studio).
Konten buat Instagram, dua format yang bisa dipilih tiap ide:
- Feed: carousel evergreen, tersimpan di grid & gampang ditemukan lewat search/profil. Cocok buat
  tutorial/case-study/insight yang perlu disimpan & dibaca ulang.
- Story: konten cepat & spontan, cocok quick tips/teaser, otomatis hilang dalam 24 jam.
Tone: edukatif tapi engaging, bahasa Indonesia santai-profesional.

Tanggal hari ini: {today}.

Content pillar:
{pillar_block}

Tugas kamu:
1. Untuk TIAP sumber di daftar bawah, buat tepat 1 ide di pillar yang tertulis, isi source_id = id sumbernya.
   Fakta, angka, nama fitur/produk, & klaim di copy WAJIB berasal dari "content" sumber itu. Kalau content-nya
   tipis, bahas secara umum -- JANGAN ngarang detail spesifik yang nggak ada di content.
2. Buat 1-2 ide pillar Case Study & Portfolio sebagai TEMPLATE dengan placeholder eksplisit
   dalam kurung siku, misal "[NAMA PROJECT]" atau "[HASIL/ANGKA]". source_id = 0.
3. Hari besar dalam 0-3 hari ke depan (hari & tanggal ini SUDAH PASTI benar -- pakai persis, jangan hitung
   ulang, jangan tambah hari besar lain):
{hari_besar}
   Buat maksimal 1 ide pillar Hari Besar per hari besar di atas, source_id = 0. Kalau "(tidak ada)", JANGAN
   buat ide pillar Hari Besar sama sekali.
4. Hook & sudut pandang jangan mirip konten {history_days} hari terakhir ini:
{recent_hooks}

Untuk tiap ide, keluarkan field:
- pillar: salah satu dari nama pillar di atas (persis sama penulisannya)
- format: "Feed" atau "Story" -- pilih yang paling cocok sama isi & bobot ide ini
- hook: satu kalimat pembuka yang nge-hook buat slide/frame pertama
- angle: penjelasan sudut pandang/kenapa ide ini menarik buat audiens sekarang (1-2 kalimat)
- outline: array 5-7 slide/frame, tiap slide berupa object {{"slide_title": judul singkat slide, "copy": draft
  copywriting SIAP PAKAI untuk slide itu (2-4 kalimat, bukan cuma judul)}}
- source_id: id sumber dari daftar di bawah, atau 0 untuk Case Study & Portfolio / Hari Besar

Balas HANYA JSON array sesuai schema. Daftar sumber:
{sources}"""


def tanggal_id(d):
    return f"{HARI_ID[d.weekday()]}, {d.day} {BULAN_ID[d.month - 1]} {d.year}"


def upcoming_hari_besar(today, days=3):
    """Hari besar dari hari ini s/d `days` hari ke depan, lengkap dengan hari & tanggal yang benar."""
    out = []
    for n in range(days + 1):
        d = today + datetime.timedelta(days=n)
        name = HARI_BESAR.get(d.strftime("%m-%d"))
        if name:
            out.append(f"{name} -- {tanggal_id(d)} ({'hari ini' if n == 0 else f'H-{n}'})")
    return out


def load_history(today, days=HISTORY_DAYS, folder="research"):
    """Dari output riset sebelumnya (tidak termasuk hari ini, biar re-run manual tetap bebas):
    (semua link sumber yang pernah dipakai, hook ide `days` hari terakhir)."""
    used_links, recent_hooks = set(), []
    for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
        try:
            age = (today - datetime.date.fromisoformat(os.path.basename(path)[:-3])).days
        except ValueError:
            continue
        if age <= 0:
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        used_links.update(re.findall(r"\[Sumber\]\((https?://[^)]+)\)", text))
        if age <= days:
            recent_hooks += re.findall(r"^### \d+\. (.+?) _\(Format", text, re.M)
    return used_links, recent_hooks


def _bullets(lines):
    return "\n".join(f"   - {x}" for x in lines) or "   (tidak ada)"


def _pillar_block(names):
    return "\n".join(f"- {n} -- {PILLARS[n][0]} Tujuan: {PILLARS[n][1]}." for n in names)


def get_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY belum di-set (env var / GitHub secret).")
    # default SDK: tanpa timeout + 5x retry -> model yang overload bisa bikin run menggantung sampai job
    # di-kill. Timeout + 1x retry bikin gagal cepat, lalu _generate pindah ke model berikutnya.
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        timeout=REQUEST_TIMEOUT_S * 1000,
        retry_options=types.HttpRetryOptions(attempts=2),
    ))


def _generate(client, prompt, schema):
    for model in MODELS:
        started = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            print(f"[agent] {model} selesai dalam {time.monotonic() - started:.0f} detik")
            break
        except (errors.APIError, httpx.TimeoutException) as exc:  # timeout dilempar httpx, bukan APIError
            retryable = isinstance(exc, httpx.TimeoutException) or exc.code in RETRYABLE_CODES
            if not retryable or model == MODELS[-1]:
                raise
            print(f"[agent] {model} gagal setelah {time.monotonic() - started:.0f} detik "
                  f"({type(exc).__name__}: {str(exc)[:80]}), fallback ke model berikutnya")
    try:
        data = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"Gagal parse JSON dari Gemini: {exc}\nRaw response: {resp.text!r}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Response Gemini bukan JSON array: {data!r}")
    return data, model


def collect(used_links):
    items = fetch_rss()
    try:
        items += fetch_github_trending()
    except Exception as exc:
        print(f"[agent] gagal fetch GitHub trending: {exc}")

    # dedup per judul/link + buang yang sudah pernah jadi sumber konten di hari-hari sebelumnya
    seen, uniq = set(used_links), []
    for it in items:
        if it["title"] in seen or it["link"] in seen:
            continue
        seen.update((it["title"], it["link"]))
        uniq.append(it)
    print(f"[agent] total {len(uniq)} sumber unik & belum pernah dipakai")
    return uniq


def select(client, items, today, recent_hooks):
    """Tahap 1: Gemini cuma milih sumber terbaik per pillar (belum nulis apa-apa)."""
    if not items:
        raise RuntimeError("Tidak ada sumber yang berhasil di-fetch, skip synthesize.")
    prompt = SELECT_PROMPT.format(
        today=tanggal_id(today),
        pillar_block="\n".join(f"- {n} (pilih {lo}-{hi} sumber) -- {PILLARS[n][0]}"
                               for n, (lo, hi) in IDEAS_PER_PILLAR.items()),
        history_days=HISTORY_DAYS,
        recent_hooks=_bullets(recent_hooks),
        sources=json.dumps([{"id": i, **it} for i, it in enumerate(items)], ensure_ascii=False),
    )
    picks, used_ids, per_pillar = [], set(), {}
    data, model = _generate(client, prompt, SELECT_SCHEMA)
    for p in data:
        i, pillar = p.get("id"), p.get("pillar")
        if not isinstance(i, int) or not 0 <= i < len(items) or i in used_ids or pillar not in GROUNDED_PILLARS:
            print(f"[agent] abaikan pilihan tidak valid: {p!r}")
            continue
        if per_pillar.get(pillar, 0) >= IDEAS_PER_PILLAR[pillar][1]:
            continue
        used_ids.add(i)
        per_pillar[pillar] = per_pillar.get(pillar, 0) + 1
        picks.append({**items[i], "pillar": pillar})
        print(f"[agent] pilih [{pillar}] {items[i]['title']} -- {p.get('why', '')}")
    return picks, model


def _validate_idea(i, idea):
    missing = [f for f in REQUIRED_FIELDS if f not in idea]
    if missing:
        raise RuntimeError(f"Ide #{i} kehilangan field {missing}: {idea!r}")
    if idea["pillar"] not in PILLAR_NAMES:
        raise RuntimeError(f"Ide #{i} pakai pillar tidak dikenal: {idea['pillar']!r}")
    if idea["format"] not in FORMATS:
        raise RuntimeError(f"Ide #{i} pakai format tidak dikenal: {idea['format']!r}")
    if not isinstance(idea["outline"], list) or not idea["outline"]:
        raise RuntimeError(f"Ide #{i} outline kosong/tidak valid: {idea!r}")
    for slide in idea["outline"]:
        if "slide_title" not in slide or "copy" not in slide:
            raise RuntimeError(f"Ide #{i} ada slide tanpa slide_title/copy: {slide!r}")


def write(client, picks, today, recent_hooks, hari_besar):
    """Tahap 2: tulis ide + copy berdasarkan isi artikel yang dipilih. Link sumber diisi kode dari
    source_id, jadi Gemini nggak bisa ngarang URL."""
    sources = []
    for n, p in enumerate(picks, start=1):
        text = fetch_article(p["link"])
        sources.append({
            "id": n,
            "pillar": p["pillar"],
            "title": p["title"],
            "published": p["published"],
            "content": text if len(text) > len(p["summary"]) else p["summary"],
        })
    prompt = WRITE_PROMPT.format(
        today=tanggal_id(today),
        pillar_block=_pillar_block(PILLAR_NAMES),
        hari_besar=_bullets(hari_besar),
        history_days=HISTORY_DAYS,
        recent_hooks=_bullets(recent_hooks),
        sources=json.dumps(sources, ensure_ascii=False),
    )
    links = {n: p["link"] for n, p in enumerate(picks, start=1)}
    ideas = []
    data, model = _generate(client, prompt, WRITE_SCHEMA)
    for i, idea in enumerate(data):
        _validate_idea(i, idea)
        sid = idea.pop("source_id")
        if idea["pillar"] == "Hari Besar" and not hari_besar:
            print(f"[agent] buang ide #{i}: Hari Besar padahal tidak ada hari besar dekat")
            continue
        if idea["pillar"] in PLACEHOLDER_PILLARS:
            idea["source"] = "-"
        elif sid in links:
            idea["source"] = links[sid]
        else:
            print(f"[agent] buang ide #{i}: source_id {sid} tidak ada di daftar sumber ({idea['hook']!r})")
            continue
        ideas.append(idea)
    if not ideas:
        raise RuntimeError("Semua ide dari Gemini tidak valid, tidak ada yang disimpan.")

    counts = {}
    for idea in ideas:
        counts[idea["pillar"]] = counts.get(idea["pillar"], 0) + 1
    for name, (lo, hi) in IDEAS_PER_PILLAR.items():
        n = counts.get(name, 0)
        if not lo <= n <= hi:
            print(f"[agent] peringatan: pillar '{name}' dapat {n} ide (di luar target {lo}-{hi})")

    print(f"[agent] synthesize {len(ideas)} ide konten -- {counts}")
    return ideas, model


def _render_markdown(ideas):
    by_pillar = {name: [] for name in PILLAR_NAMES}
    for idea in ideas:
        by_pillar[idea["pillar"]].append(idea)

    parts = []
    for pillar in PILLAR_NAMES:
        items = by_pillar[pillar]
        if not items:
            continue
        placeholder = pillar in PLACEHOLDER_PILLARS
        parts.append(f"## {pillar}\n\n")
        if placeholder:
            parts.append("> ⚠️ **TEMPLATE — isi manual sebelum posting** (tidak ada data sumber asli untuk pillar ini)\n\n")
        for n, idea in enumerate(items, start=1):
            fmt = idea["format"]
            parts.append(f"### {n}. {idea['hook']} _(Format: {fmt} — {FORMATS[fmt]})_\n\n")
            parts.append(f"**Angle:** {idea['angle']}\n\n")
            parts.append("**Slide-by-slide:**\n")
            for s_n, s in enumerate(idea["outline"], start=1):
                parts.append(f"{s_n}. **{s['slide_title']}** — {s['copy']}\n")
            source = (idea.get("source") or "-").strip()
            if source and source != "-":
                parts.append(f"\n[Sumber]({source})\n\n")
            else:
                parts.append("\nSumber: - (internal)\n\n")
            parts.append("---\n\n")
    return "".join(parts)


def _header(today, meta):
    return f"# content_{today.isoformat()}\n\n" + "".join(f"- **{k}:** {v}\n" for k, v in meta.items()) + "\n---\n\n"


def save(ideas, today, meta):
    os.makedirs("research", exist_ok=True)
    path = f"research/{today.isoformat()}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(_header(today, meta))
        f.write(_render_markdown(ideas))
    print(f"[agent] tersimpan di {path}")
    return path


def write_summary(ideas, today, meta):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return  # lagi jalan lokal, skip
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(_header(today, meta))
        f.write(_render_markdown(ideas))


def notify(ideas):
    hook_url = os.environ.get("DISCORD_WEBHOOK")
    if not hook_url:
        return
    msg = "**Ide konten hari ini**\n" + "\n".join(f"• [{i['pillar']}] {i['hook']}" for i in ideas)
    try:
        httpx.post(hook_url, json={"content": msg}, timeout=20)
    except Exception as exc:
        print(f"[agent] gagal kirim notifikasi Discord (diabaikan, tidak menggagalkan run): {exc}")


if __name__ == "__main__":
    today = datetime.date.today()  # WIB: workflow set TZ=Asia/Jakarta
    client = get_client()
    used_links, recent_hooks = load_history(today)
    hari_besar = upcoming_hari_besar(today)
    items = collect(used_links)
    picks, select_model = select(client, items, today, recent_hooks)
    ideas, write_model = write(client, picks, today, recent_hooks, hari_besar)
    meta = {
        "Tanggal": tanggal_id(today),
        "Di-generate": datetime.datetime.now().astimezone().strftime("%H:%M %Z"),
        "Model": select_model if select_model == write_model
                 else f"{select_model} (pilih sumber), {write_model} (tulis konten)",
        "Sumber": f"{len(items)} kandidat fresh → {len(picks)} dipilih",
        "Hari besar dekat": "; ".join(hari_besar) or "-",
    }
    save(ideas, today, meta)
    write_summary(ideas, today, meta)
    notify(ideas)   # opsional, best-effort, tidak menggagalkan run kalau error
