import os, json, datetime, httpx
from google import genai
from google.genai import types
from sources import fetch_rss, fetch_github_trending

MIN_IDEAS_PER_PILLAR = 1
MAX_IDEAS_PER_PILLAR = 3

# format konten -> keterangan kegunaannya (ditampilin di samping judul tiap ide)
FORMATS = {
    "Feed": "carousel evergreen, tersimpan di grid & gampang ditemukan lewat search/profil",
    "Story": "konten cepat & spontan, cocok quick tips/teaser, otomatis hilang dalam 24 jam",
}

# pillar -> (deskripsi, rasio target %, tujuan, ada data sumber asli?)
PILLARS = {
    "Tech & Tutorial": ("Tips & trik praktis buat developer sehari-hari, rekomendasi tools/website & AI assistant kekinian yang beneran berguna, produktivitas & soft skill kerja dev. Mini-tutorial framework (Laravel, Next.js, FastAPI) boleh sesekali tapi bukan mayoritas.", 25, "Bangun kredibilitas teknis", True),
    "Design & UI/UX": ("Proses desain, breakdown UI, tips Figma, before/after.", 25, "Tunjukin skill desain", True),
    "Case Study & Portfolio": ("Showcase project, hasil kerja, cerita di balik project.", 25, "Konversi ke klien", False),
    "IT & Tech Industry": ("Berita/tren industri IT non-teknis -- bisnis software, AI di dunia kerja, produk/startup, arah industri.", 25, "Relevansi & insight industri", True),
    "Hari Besar": ("Konten menyambut hari besar/perayaan nasional atau internasional yang dekat tanggal hari ini.", None, "Merayakan Hari Besar", False),
}
PILLAR_NAMES = list(PILLARS.keys())
PLACEHOLDER_PILLARS = {name for name, meta in PILLARS.items() if not meta[3]}

REQUIRED_FIELDS = ("pillar", "format", "hook", "angle", "outline", "source")

RESPONSE_SCHEMA = {
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
            "source": {"type": "string"},
        },
        "required": list(REQUIRED_FIELDS),
    },
}


def _pillar_prompt_block():
    lines = []
    for i, (name, (desc, ratio, goal, grounded)) in enumerate(PILLARS.items(), start=1):
        ratio_txt = f"{ratio}%" if ratio is not None else "kondisional"
        note = (
            "WAJIB berdasarkan salah satu item di daftar sumber di bawah."
            if grounded else
            "TIDAK ada data asli tersedia -- buat sebagai TEMPLATE dengan placeholder eksplisit dalam kurung siku, "
            "misal \"[NAMA PROJECT]\" atau \"[CERITA HARI INI]\"."
        )
        if name == "Hari Besar":
            note = ("HANYA pilih pillar ini kalau ada hari besar/perayaan nasional atau internasional yang jatuh "
                    "dalam 1-3 hari dari tanggal hari ini. Kalau tidak ada, JANGAN pakai pillar ini sama sekali.")
        elif name == "Tech & Tutorial":
            note += (" Prioritaskan bahasa yang gampang dicerna, jangan jargon-heavy atau deep-dive kode yang "
                      "cuma dimengerti senior dev -- fokus ke value praktis (tips, tools, produktivitas). "
                      "Mini-tutorial kode spesifik boleh muncul sesekali tapi jangan jadi mayoritas ide di pillar ini.")
        lines.append(f"{i}. {name} ({ratio_txt}) -- {desc} Tujuan: {goal}. {note}")
    return "\n".join(lines)


PROMPT = """Kamu content strategist buat brand xharp.dev (software development & design studio).
Konten buat Instagram, dua format yang bisa dipilih tiap ide:
- Feed: carousel evergreen, tersimpan di grid & gampang ditemukan lewat search/profil. Cocok buat
  tutorial/case-study/insight yang perlu disimpan & dibaca ulang.
- Story: konten cepat & spontan, cocok quick tips/teaser, otomatis hilang dalam 24 jam.
Tone: edukatif tapi engaging, bahasa Indonesia santai-profesional.

Tanggal hari ini: {today}.

Ada {n_pillars} content pillar dengan rasio target (pilih pillar secara weighted-random mengikuti rasio ini
dalam jangka beberapa minggu -- TIDAK harus persis rasionya tiap hari):

{pillar_block}

Tugas kamu: generate ide konten hari ini PER PILLAR:
- Tiap pillar (kecuali Hari Besar) hasilkan {min_per_pillar}-{max_per_pillar} ide. Sesuaikan jumlahnya
  sama ketersediaan & kualitas sumber yang relevan -- jangan maksain jumlah kalau sumbernya tipis.
- Pillar Hari Besar diisi maksimal {max_per_pillar} ide HANYA kalau ada hari besar/perayaan yang jatuh
  dalam 1-3 hari dari tanggal hari ini. Kalau tidak ada, JANGAN sertakan ide apapun buat pillar ini.
- Khususnya jangan melulu soal API/backend generik.

Untuk tiap ide, keluarkan field:
- pillar: salah satu dari nama pillar di atas (persis sama penulisannya)
- format: "Feed" atau "Story" -- pilih yang paling cocok sama isi & bobot ide ini
- hook: satu kalimat pembuka yang nge-hook buat slide/frame pertama
- angle: penjelasan sudut pandang/kenapa ide ini menarik (1-2 kalimat)
- outline: array 5-7 slide/frame, tiap slide berupa object {{"slide_title": judul singkat slide, "copy": draft
  copywriting SIAP PAKAI untuk slide itu (2-4 kalimat, bukan cuma judul)}}
- source: link URL sumber yang jadi dasar ide (wajib untuk pillar yang butuh sumber asli). Kalau pillar
  Case Study & Portfolio tanpa sumber eksternal, isi dengan "-".

Balas HANYA JSON array sesuai schema. Daftar sumber:
{sources}"""


def get_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY belum di-set (env var / GitHub secret).")
    return genai.Client(api_key=api_key)


def collect():
    items = fetch_rss()
    try:
        items += fetch_github_trending()
    except Exception as exc:
        print(f"[agent] gagal fetch GitHub trending: {exc}")

    seen, uniq = set(), []
    for it in items:
        if it["title"] not in seen:
            seen.add(it["title"])
            uniq.append(it)
    print(f"[agent] total {len(uniq)} sumber unik terkumpul")
    return uniq


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


def synthesize(client, items):
    if not items:
        raise RuntimeError("Tidak ada sumber yang berhasil di-fetch, skip synthesize.")

    prompt = PROMPT.format(
        today=datetime.date.today().isoformat(),
        n_pillars=len(PILLARS),
        pillar_block=_pillar_prompt_block(),
        min_per_pillar=MIN_IDEAS_PER_PILLAR,
        max_per_pillar=MAX_IDEAS_PER_PILLAR,
        sources=json.dumps(items, ensure_ascii=False),
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",   # cek nama model terbaru di docs Gemini
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )

    try:
        ideas = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"Gagal parse JSON dari Gemini: {exc}\nRaw response: {resp.text!r}") from exc

    if not isinstance(ideas, list) or not ideas:
        raise RuntimeError(f"Response Gemini bukan list ide yang valid: {ideas!r}")
    for i, idea in enumerate(ideas):
        _validate_idea(i, idea)

    counts = {}
    for idea in ideas:
        counts[idea["pillar"]] = counts.get(idea["pillar"], 0) + 1
    for name in PILLAR_NAMES:
        n = counts.get(name, 0)
        if name != "Hari Besar" and not (MIN_IDEAS_PER_PILLAR <= n <= MAX_IDEAS_PER_PILLAR):
            print(f"[agent] peringatan: pillar '{name}' dapat {n} ide (di luar target {MIN_IDEAS_PER_PILLAR}-{MAX_IDEAS_PER_PILLAR})")

    print(f"[agent] synthesize {len(ideas)} ide konten -- {counts}")
    return ideas


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


def save(ideas):
    today = datetime.date.today().isoformat()
    os.makedirs("research", exist_ok=True)
    path = f"research/{today}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# content_{today}\n\n")
        f.write(_render_markdown(ideas))
    print(f"[agent] tersimpan di {path}")
    return path


def write_summary(ideas):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return  # lagi jalan lokal, skip
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(f"# content_{datetime.date.today().isoformat()}\n\n")
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
    client = get_client()
    ideas = synthesize(client, collect())
    save(ideas)
    write_summary(ideas)
    notify(ideas)   # opsional, best-effort, tidak menggagalkan run kalau error
