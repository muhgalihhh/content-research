"""Cek cepat logika yang nggak butuh Gemini/network: `python test_agent.py` (atau pytest)."""
import datetime, json, os, tempfile
import agent


def test_hari_besar_tanggal_pasti():
    # 27 Okt 2026 (Selasa) = H-1 Sumpah Pemuda -- dulu kelewat karena ngandelin ingatan Gemini
    assert agent.upcoming_hari_besar(datetime.date(2026, 10, 27)) == [
        "Hari Sumpah Pemuda -- Rabu, 28 Oktober 2026 (H-1)"]
    assert agent.upcoming_hari_besar(datetime.date(2026, 10, 10)) == [
        "Hari Kesehatan Mental Sedunia -- Sabtu, 10 Oktober 2026 (hari ini)"]
    assert agent.upcoming_hari_besar(datetime.date(2026, 10, 20)) == []


def test_load_history():
    with tempfile.TemporaryDirectory() as d:
        files = {
            "2026-10-20.md": "### 1. Hook lama _(Format: Feed — x)_\n[Sumber](https://a.com/old)\n",
            "2026-10-26.md": "### 1. Hook kemarin _(Format: Story — x)_\n[Sumber](https://a.com/kemarin)\n",
            "2026-10-27.md": "### 1. Hook hari ini _(Format: Feed — x)_\n[Sumber](https://a.com/today)\n",
        }
        for name, text in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        links, hooks = agent.load_history(datetime.date(2026, 10, 27), days=3, folder=d)
    assert links == {"https://a.com/old", "https://a.com/kemarin"}  # hari ini diabaikan (re-run bebas)
    assert hooks == ["Hook kemarin"]  # cuma yang dalam 3 hari


class _FakeClient:
    """Pengganti genai.Client: balikin JSON yang sudah disiapkan."""
    def __init__(self, payload):
        resp = type("R", (), {"text": json.dumps(payload)})()
        self.models = type("M", (), {"generate_content": lambda *a, **k: resp})()


def test_generate_fallback_saat_model_overload():
    calls = []
    def gen(model, **k):
        calls.append(model)
        if len(calls) == 1:
            raise agent.errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})
        return type("R", (), {"text": "[]"})()
    client = type("C", (), {"models": type("M", (), {"generate_content": staticmethod(gen)})()})()
    assert agent._generate(client, "p", {}) == []
    assert calls == agent.MODELS[:2]


def _idea(pillar, source_id):
    return {"pillar": pillar, "format": "Feed", "hook": f"hook {pillar} {source_id}", "angle": "a",
            "outline": [{"slide_title": "t", "copy": "c"}], "source_id": source_id}


def test_write_link_dari_kode_bukan_gemini():
    agent.fetch_article = lambda url: ""  # tanpa network
    picks = [{"title": "T", "link": "https://real.com/x", "published": "2026-10-27", "summary": "s",
              "pillar": "Tech & Tutorial"}]
    client = _FakeClient([
        _idea("Tech & Tutorial", 1),          # valid -> link asli
        _idea("Tech & Tutorial", 99),         # source_id ngarang -> dibuang
        _idea("Case Study & Portfolio", 0),   # template -> "-"
        _idea("Hari Besar", 0),               # tidak ada hari besar -> dibuang
    ])
    ideas = agent.write(client, picks, datetime.date(2026, 10, 20), [], [])
    assert [(i["pillar"], i["source"]) for i in ideas] == [
        ("Tech & Tutorial", "https://real.com/x"), ("Case Study & Portfolio", "-")]


def test_select_buang_id_tidak_valid():
    items = [{"title": f"t{i}", "link": f"https://x.com/{i}", "published": "", "summary": ""} for i in range(2)]
    client = _FakeClient([
        {"id": 0, "pillar": "Tech & Tutorial", "why": "ok"},
        {"id": 0, "pillar": "Design & UI/UX", "why": "dobel"},
        {"id": 7, "pillar": "Tech & Tutorial", "why": "out of range"},
        {"id": 1, "pillar": "IT & Tech Industry", "why": "ok"},
    ])
    picks = agent.select(client, items, datetime.date(2026, 10, 27), [])
    assert [(p["link"], p["pillar"]) for p in picks] == [
        ("https://x.com/0", "Tech & Tutorial"), ("https://x.com/1", "IT & Tech Industry")]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
