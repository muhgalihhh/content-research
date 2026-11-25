import os, re, json, glob, time, datetime, httpx
from google import genai
from google.genai import errors, types
from urllib.parse import urlparse
from sources import fetch_rss, fetch_github_trending, fetch_article, FEED_STATUS

# urutan fallback kalau model di depannya overload (503) / kena kuota (429). Semua ada di free tier;
# "gemini-4-argon" lebih baru tapi butuh billing. Cek: https://ai.google.dev/gemini-api/docs/models
MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
RETRYABLE_CODES = {429, 500, 503, 504}
REQUEST_TIMEOUT_S = 120  # tahap tulis (brief ~10-14 ide) normalnya < 1 menit
# 503 "high demand" biasanya sementara (24 Nov 2026 semua model 503 -> riset hari itu hilang). Kalau semua
# model gagal, tunggu lalu coba lagi semua model dari awal, sampai GENERATE_ROUNDS putaran.
GENERATE_ROUNDS = 3
ROUND_WAIT_S = 90
# target jumlah ide per hari (min, max) per pillar bersumber; Tech & Tutorial pillar utama -> jatah terbanyak
IDEAS_PER_PILLAR = {"Tech & Tutorial": (3, 5), "Design & UI/UX": (1, 3), "IT & Tech Industry": (1, 3)}
HISTORY_DAYS = 7  # hook N hari terakhir dikirim ke Gemini supaya topiknya nggak diulang-ulang

# format konten -> keterangan kegunaannya (ditampilin di samping judul tiap ide).
# Fokus IG: Reels (jangkauan ke non-follower) + Feed carousel (di-save & dibaca ulang).
FORMATS = {
    "Reels": "video vertikal 15-45 detik, jangkauan terluas ke non-follower lewat tab Reels/Explore",
    "Feed": "carousel evergreen, gampang di-save, dibaca ulang & ditemukan lewat profil",
}
MIN_REELS_SHARE = 0.5  # minimal separuh ide bersumber harus Reels; di bawah ini cuma warning di log

# Variasi gaya hook supaya feed nggak monoton. Gemini wajib pilih satu per ide & dilarang pakai gaya yang
# sama lebih dari MAX_SAME_HOOK_STYLE kali per hari (dicek kode, kelebihan -> warning).
HOOK_STYLES = {
    "Hasil duluan": "tunjukin hasil akhir/demo di detik pertama, baru jelasin caranya",
    "Mitos vs fakta": "patahkan anggapan umum yang salah",
    "Kesalahan umum": "'X kesalahan yang bikin ...' -- audiens ngecek apakah mereka ngelakuin",
    "Angka spesifik": "angka/hasil konkret dari sumber (bukan angka karangan)",
    "Perbandingan": "A vs B, before/after, cara lama vs cara baru",
    "POV / relatable": "situasi sehari-hari dev/desainer yang bikin 'ini gue banget'",
    "Opini berani": "sikap tegas yang bisa diperdebatkan, ngajak komen",
    "Jarang diketahui": "fitur/trik tersembunyi yang kebanyakan orang belum tahu",
    "Berita + dampak": "kabar terbaru langsung disambung ke 'artinya buat kamu ...'",
}
MAX_SAME_HOOK_STYLE = 2
# Pembuka yang kepake terus-terusan di riset lama -> bikin semua konten kedengeran sama. Dilarang di prompt.
BANNED_OPENERS = ["Pernah ngerasa", "Pernah ngalamin", "Pernah kesal", "Pernah khawatir", "Bingung",
                  "Pengen", "Yuk", "Hati-hati!", "Lagi pusing", "Bosan", "Masih pakai", "Penasaran"]

# Maksimal sumber dari domain yang sama per hari (Smashing dulu nyumbang >50% ide Design).
# Platform multi-penulis boleh lebih karena isinya dari orang/project yang beda-beda.
MAX_PER_DOMAIN = 1
MAX_PER_DOMAIN_OVERRIDE = {"dev.to": 2, "github.com": 2}

PORTFOLIO_FILE = "portfolio.json"  # opsional; kalau ada, Case Study pakai project asli, bukan template

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

# Output = brief ide, bukan naskah. Naskah lengkap ditulis terpisah (GPT) dari blok "Brief buat GPT".
REQUIRED_FIELDS = ("pillar", "format", "hook_style", "hook", "angle", "why_now", "key_facts",
                   "score", "source_id")

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
            "hook_style": {"type": "string", "enum": list(HOOK_STYLES.keys())},
            "hook": {"type": "string"},
            "angle": {"type": "string"},
            "why_now": {"type": "string"},
            "key_facts": {"type": "array", "items": {"type": "string"}},
            "score": {"type": "integer"},
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
3. Bisa jadi konten yang MENARIK DILIHAT, bukan cuma dibaca: ada yang bisa didemoin, dibandingin
   before/after, ditunjukin hasilnya di layar, atau punya angka/fakta yang bikin kaget. Konten akan jadi
   Reels & carousel Instagram, jadi sumber yang isinya cuma teori panjang tanpa hal konkret = prioritas rendah.
4. Topik yang muncul di beberapa sumber sekaligus berarti lagi hangat -- prioritaskan.
5. JANGAN pilih topik yang sama/mirip dengan konten {history_days} hari terakhir ini:
{recent_hooks}

Jumlah sumber per pillar ikuti angka "pilih X-Y" di tiap pillar. Boleh di bawah minimum HANYA kalau memang
nggak ada kandidat yang layak -- jangan pilih sumber jelek demi memenuhi jumlah. Di pillar yang slotnya banyak,
variasikan sub-topiknya (mis. tools/AI assistant, tips produktivitas, tutorial singkat), jangan semua sejenis.
Variasikan juga asal sumbernya: maksimal 1 sumber dari situs yang sama (kecuali dev.to & github.com maks 2).
Satu sumber hanya boleh dipilih sekali. Isi "why" dengan 1 kalimat alasan memilih.
Balas HANYA JSON array sesuai schema. Daftar sumber (pakai "id"-nya):
{sources}"""

WRITE_PROMPT = """Kamu content strategist Instagram brand xharp.dev (software development & design studio).
Audiens: developer, desainer, & orang yang tertarik dunia IT di Indonesia.
Tugasmu BUKAN nulis naskah, tapi bikin BRIEF IDE: hook, angle, & fakta kunci yang nanti dipakai penulis
naskah lain (yang TIDAK membaca artikel sumbernya). Jadi fakta kunci harus lengkap & akurat.

Dua format:
- Reels: video vertikal 15-45 detik. Tujuannya jangkauan ke orang yang BELUM follow, jadi 3 detik pertama
  harus bikin berhenti scroll. Cocok buat demo, before/after, tips cepat, berita + dampaknya, opini.
- Feed: carousel yang bikin orang nge-SAVE. Cocok buat step-by-step, cheat sheet, list, perbandingan detail.
Minimal setengah ide bersumber harus Reels. Pilih Feed kalau isinya memang perlu dibaca pelan/disimpan.

Tanggal hari ini: {today}.

Content pillar:
{pillar_block}

ATURAN BIAR NGGAK BORING (wajib):
- Hook = janji yang spesifik. Sebut hal konkret (nama tool, angka dari sumber, hasil yang didapat), bukan
  kalimat umum. Jelek: "Ada tools AI keren nih!". Bagus: "Tool ini bikin 1 komponen Figma jadi kode React
  dalam 10 detik."
- JANGAN buka hook dengan: {banned_openers}. Jangan pakai pertanyaan retoris "Pernah ...?" sama sekali.
- Tiap ide pilih satu hook_style dari daftar ini, dan satu gaya maksimal {max_same_style}x hari ini:
{hook_styles}
- Bahasa ngobrol, kayak dev ngomong ke temen. Tanpa basa-basi ("Di era digital ini...").
- Angle = sudut pandang yang bikin ide ini beda, bukan ringkasan isi sumber.

Tugas kamu:
1. Untuk TIAP sumber di daftar bawah, buat tepat 1 ide di pillar yang tertulis, isi source_id = id sumbernya.
   Fakta, angka, nama fitur/produk, & klaim WAJIB berasal dari "content" sumber itu. Kalau content-nya
   tipis, tulis fakta yang ada saja (boleh cuma 1-2) -- JANGAN ngarang detail yang nggak ada di content.
2. Buat 1 ide pillar Case Study & Portfolio, source_id = 0.
{portfolio}
3. Hari besar dalam 0-3 hari ke depan (hari & tanggal ini SUDAH PASTI benar -- pakai persis, jangan hitung
   ulang, jangan tambah hari besar lain):
{hari_besar}
   Buat maksimal 1 ide pillar Hari Besar per hari besar di atas, source_id = 0. Sambungkan ke dunia dev/desain
   (bukan ucapan selamat generik). Kalau "(tidak ada)", JANGAN buat ide pillar Hari Besar sama sekali.
4. Hook & sudut pandang jangan mirip konten {history_days} hari terakhir ini:
{recent_hooks}

Untuk tiap ide, keluarkan field:
- pillar: salah satu dari nama pillar di atas (persis sama penulisannya)
- format: "Reels" atau "Feed"
- hook_style: salah satu nama gaya hook di atas (persis)
- hook: kalimat pembuka (Reels: 3 detik pertama; Feed: judul slide pertama)
- angle: sudut pandang ide ini & apa yang audiens dapat (1-2 kalimat)
- why_now: 1 kalimat kenapa relevan HARI INI (rilis baru, tren, kejadian minggu ini, hari besar)
- key_facts: 3-5 poin fakta dari sumber yang dibutuhkan buat nulis naskah: angka, nama fitur/produk/tool,
  langkah/cara pakai, perbandingan, kutipan klaim penting. Tiap poin 1 kalimat, berdiri sendiri, tanpa
  opini. Case Study/Hari Besar: poin yang perlu diisi/disiapkan (placeholder [..] kalau datanya belum ada).
- score: 1-10, potensi engagement di IG (seberapa bikin berhenti scroll + layak di-save/share). Jujur,
  jangan semua 8-9.
- source_id: id sumber dari daftar di bawah, atau 0 untuk Case Study & Portfolio / Hari Besar

Balas HANYA JSON array sesuai schema. Daftar sumber:
{sources}"""

# Blok siap tempel ke GPT buat nulis naskah lengkap. {format_task} beda per format.
GPT_BRIEF = """Akun: Instagram @xharp.dev (software development & design studio)
Audiens: developer, desainer, & orang yang tertarik dunia IT di Indonesia
Gaya: bahasa Indonesia santai-profesional, kayak dev ngobrol ke temen, tanpa basa-basi

Tugas: tulis {format_task}.

Hook: {hook}
Angle: {angle}
Kenapa sekarang: {why_now}

Fakta kunci (pakai HANYA fakta ini, jangan tambah klaim/angka lain):
{facts}

Sumber: {source}

Sertakan juga: caption IG (2-4 kalimat), 1 CTA spesifik (bukan "follow untuk info lainnya"), & 5-8 hashtag."""

GPT_FORMAT_TASK = {
    "Reels": "naskah Reels 15-45 detik, per scene: durasi, voiceover, teks di layar, & arahan visual. "
             "Scene pertama = hook (maks 3 detik), scene terakhir = payoff + CTA",
    "Feed": "copy carousel Instagram 5-8 slide, per slide: judul, isi (1-3 kalimat pendek), & arahan visual. "
            "Slide terakhir = takeaway + CTA",
}

PORTFOLIO_TEMPLATE_RULE = """   Belum ada data project asli -> buat sebagai TEMPLATE dengan placeholder eksplisit dalam kurung siku, misal
   "[NAMA PROJECT]" atau "[HASIL/ANGKA]". Hindari pembuka klise "Dari ide mentah jadi ..."; pakai angle
   spesifik seperti satu keputusan desain, satu masalah teknis yang dipecahkan, atau before/after."""

PORTFOLIO_REAL_RULE = """   Pakai project asli xharp.dev di bawah ini (cuma project ini, jangan campur project lain). Fakta & angka
   WAJIB dari data ini; yang nggak ada di data ditulis sebagai placeholder [DALAM KURUNG SIKU], jangan ngarang.
   Pilih satu sisi yang paling menarik (keputusan desain, tantangan teknis, before/after), jangan semuanya:
   {project}"""


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


def load_portfolio(today, path=PORTFOLIO_FILE):
    """Satu project dari portfolio.json buat hari ini (dirotasi per tanggal), atau None kalau file belum ada."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        projects = json.load(f)
    if not isinstance(projects, list) or not projects:
        return None
    return projects[today.toordinal() % len(projects)]


def _domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


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


def _generate(client, prompt, schema, rounds=GENERATE_ROUNDS, wait_s=ROUND_WAIT_S, sleep=time.sleep):
    resp = None
    for rnd in range(1, rounds + 1):
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
                if not retryable or (model == MODELS[-1] and rnd == rounds):
                    raise
                print(f"[agent] {model} gagal setelah {time.monotonic() - started:.0f} detik "
                      f"({type(exc).__name__}: {str(exc)[:80]}), fallback ke model berikutnya")
        if resp is not None:
            break
        print(f"[agent] semua model gagal di putaran {rnd}/{rounds}, tunggu {wait_s} detik lalu coba lagi")
        sleep(wait_s)
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
    picks, used_ids, per_pillar, per_domain = [], set(), {}, {}
    data, model = _generate(client, prompt, SELECT_SCHEMA)
    for p in data:
        i, pillar = p.get("id"), p.get("pillar")
        if not isinstance(i, int) or not 0 <= i < len(items) or i in used_ids or pillar not in GROUNDED_PILLARS:
            print(f"[agent] abaikan pilihan tidak valid: {p!r}")
            continue
        if per_pillar.get(pillar, 0) >= IDEAS_PER_PILLAR[pillar][1]:
            continue
        dom = _domain(items[i]["link"])
        if per_domain.get(dom, 0) >= MAX_PER_DOMAIN_OVERRIDE.get(dom, MAX_PER_DOMAIN):
            print(f"[agent] skip {items[i]['title']!r}: kuota domain {dom} hari ini sudah penuh")
            continue
        used_ids.add(i)
        per_pillar[pillar] = per_pillar.get(pillar, 0) + 1
        per_domain[dom] = per_domain.get(dom, 0) + 1
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
    facts = idea["key_facts"] if isinstance(idea["key_facts"], list) else []
    idea["key_facts"] = [str(f).strip() for f in facts if str(f).strip()]
    if not idea["key_facts"] and idea["pillar"] not in PLACEHOLDER_PILLARS:
        print(f"[agent] peringatan: ide #{i} tanpa fakta kunci, naskah GPT rawan ngarang ({idea['hook']!r})")
    idea["score"] = min(10, max(1, int(idea["score"]))) if isinstance(idea["score"], (int, float)) else 5


def _quality_warnings(ideas):
    """Cek kualitas yang nggak bisa dipaksa lewat schema. Cuma warning di log, ide tetap disimpan."""
    warns = []
    styles = {}
    for idea in ideas:
        styles[idea["hook_style"]] = styles.get(idea["hook_style"], 0) + 1
        low = idea["hook"].lower()
        if any(low.startswith(b.lower()) for b in BANNED_OPENERS):
            warns.append(f"hook pakai pembuka klise: {idea['hook']!r}")
    warns += [f"gaya hook '{s}' dipakai {n}x (maks {MAX_SAME_HOOK_STYLE})"
              for s, n in styles.items() if n > MAX_SAME_HOOK_STYLE]
    grounded = [i for i in ideas if i["pillar"] not in PLACEHOLDER_PILLARS]
    if grounded:
        share = sum(i["format"] == "Reels" for i in grounded) / len(grounded)
        if share < MIN_REELS_SHARE:
            warns.append(f"porsi Reels cuma {share:.0%} (target minimal {MIN_REELS_SHARE:.0%})")
    for w in warns:
        print(f"[agent] peringatan kualitas: {w}")
    return warns


def write(client, picks, today, recent_hooks, hari_besar, project=None):
    """Tahap 2: tulis brief ide (hook, angle, fakta kunci) berdasarkan isi artikel yang dipilih. Link sumber
    diisi kode dari source_id, jadi Gemini nggak bisa ngarang URL."""
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
        banned_openers=", ".join(f'"{b}"' for b in BANNED_OPENERS),
        max_same_style=MAX_SAME_HOOK_STYLE,
        hook_styles="\n".join(f"   - {k}: {v}" for k, v in HOOK_STYLES.items()),
        portfolio=(PORTFOLIO_REAL_RULE.format(project=json.dumps(project, ensure_ascii=False))
                   if project else PORTFOLIO_TEMPLATE_RULE),
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
    _quality_warnings(ideas)

    print(f"[agent] synthesize {len(ideas)} ide konten -- {counts}")
    return ideas, model


def gpt_brief(idea):
    """Teks siap tempel ke GPT buat nulis naskah lengkap dari brief ide ini."""
    source = (idea.get("source") or "-").strip()
    return GPT_BRIEF.format(
        format_task=GPT_FORMAT_TASK[idea["format"]],
        hook=idea["hook"], angle=idea["angle"], why_now=idea["why_now"],
        facts="\n".join(f"- {f}" for f in idea["key_facts"]) or "- (tidak ada, isi manual)",
        source=source if source != "-" else "internal xharp.dev",
    )


def _render_priorities(ideas, top=3):
    best = sorted(ideas, key=lambda i: -i["score"])[:top]
    lines = [f"{n}. **[{i['format']}] {i['hook']}** _(skor {i['score']}/10, {i['pillar']})_\n"
             for n, i in enumerate(best, start=1)]
    return "## 🔥 Prioritas hari ini\n\n" + "".join(lines) + "\n---\n\n"


def _render_markdown(ideas, project=None):
    by_pillar = {name: [] for name in PILLAR_NAMES}
    for idea in ideas:
        by_pillar[idea["pillar"]].append(idea)

    parts = [_render_priorities(ideas)] if ideas else []
    for pillar in PILLAR_NAMES:
        items = by_pillar[pillar]
        if not items:
            continue
        parts.append(f"## {pillar}\n\n")
        if pillar == "Case Study & Portfolio" and project:
            parts.append(f"> ℹ️ Dari `{PORTFOLIO_FILE}`: **{project.get('name', '?')}**. "
                         "Cek ulang fakta & isi placeholder [..] sebelum posting.\n\n")
        elif pillar in PLACEHOLDER_PILLARS:
            parts.append("> ⚠️ **TEMPLATE — isi manual sebelum posting** (tidak ada data sumber asli untuk pillar ini)\n\n")
        for n, idea in enumerate(items, start=1):
            fmt = idea["format"]
            parts.append(f"### {n}. {idea['hook']} _(Format: {fmt} — {FORMATS[fmt]})_\n\n")
            parts.append(f"**Gaya hook:** {idea['hook_style']} · **Skor:** {idea['score']}/10\n\n")
            parts.append(f"**Angle:** {idea['angle']}\n\n")
            parts.append(f"**Kenapa sekarang:** {idea['why_now']}\n\n")
            if idea["key_facts"]:
                parts.append("**Fakta kunci:**\n" + "".join(f"- {f}\n" for f in idea["key_facts"]) + "\n")
            source = (idea.get("source") or "-").strip()
            if source and source != "-":
                parts.append(f"[Sumber]({source})\n\n")
            else:
                parts.append("Sumber: - (internal)\n\n")
            # <details> + blok kode: di GitHub kelihatan rapi & ada tombol copy
            parts.append("<details><summary>📋 Brief buat GPT (buka, lalu copy)</summary>\n\n"
                         f"```text\n{gpt_brief(idea)}\n```\n\n</details>\n\n")
            parts.append("---\n\n")
    return "".join(parts)


def _header(today, meta):
    return f"# content_{today.isoformat()}\n\n" + "".join(f"- **{k}:** {v}\n" for k, v in meta.items()) + "\n---\n\n"


def save(ideas, today, meta, project=None):
    os.makedirs("research", exist_ok=True)
    path = f"research/{today.isoformat()}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(_header(today, meta))
        f.write(_render_markdown(ideas, project))
    print(f"[agent] tersimpan di {path}")
    return path


def _render_feed_health():
    """Tabel status tiap feed RSS run ini -- feed mati/pindah alamat langsung kelihatan di Actions."""
    rows = "".join(f"| {'✅' if s.startswith('ok') else '⚠️'} | {u} | {s} |\n" for u, s in FEED_STATUS.items())
    return "\n## Kesehatan feed RSS\n\n| | Feed | Status |\n|---|---|---|\n" + rows if rows else ""


def write_summary(ideas, today, meta, project=None):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return  # lagi jalan lokal, skip
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(_header(today, meta))
        f.write(_render_markdown(ideas, project))
        f.write(_render_feed_health())


def notify(ideas):
    hook_url = os.environ.get("DISCORD_WEBHOOK")
    if not hook_url:
        return
    ranked = sorted(ideas, key=lambda i: -i["score"])
    msg = "**Ide konten hari ini** (urut skor)\n" + "\n".join(
        f"• {i['score']}/10 [{i['format']}] {i['hook']}" for i in ranked)
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
    project = load_portfolio(today)
    picks, select_model = select(client, items, today, recent_hooks)
    ideas, write_model = write(client, picks, today, recent_hooks, hari_besar, project)
    meta = {
        "Tanggal": tanggal_id(today),
        "Di-generate": datetime.datetime.now().astimezone().strftime("%H:%M %Z"),
        "Model": select_model if select_model == write_model
                 else f"{select_model} (pilih sumber), {write_model} (tulis konten)",
        "Sumber": f"{len(items)} kandidat fresh → {len(picks)} dipilih",
        "Hari besar dekat": "; ".join(hari_besar) or "-",
    }
    meta["Format"] = ", ".join(f"{sum(i['format'] == f for i in ideas)} {f}" for f in FORMATS)
    save(ideas, today, meta, project)
    write_summary(ideas, today, meta, project)
    notify(ideas)   # opsional, best-effort, tidak menggagalkan run kalau error
