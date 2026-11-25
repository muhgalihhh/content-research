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
    assert agent._generate(client, "p", {}) == ([], agent.MODELS[1])  # model yang benar-benar dipakai
    assert calls == agent.MODELS[:2]

    calls.clear()  # request menggantung -> httpx timeout (bukan APIError) juga harus fallback
    def hang(model, **k):
        calls.append(model)
        if len(calls) == 1:
            raise agent.httpx.ReadTimeout("timed out")
        return type("R", (), {"text": "[]"})()
    client.models.generate_content = hang
    assert agent._generate(client, "p", {}) == ([], agent.MODELS[1])


def test_generate_retry_putaran_kalau_semua_model_overload():
    # kasus 24 Nov 2026: semua model 503 -> dulu langsung gagal; sekarang tunggu lalu putaran berikutnya
    calls, waits = [], []
    def gen(model, **k):
        calls.append(model)
        if len(calls) <= len(agent.MODELS):  # seluruh putaran pertama gagal
            raise agent.errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})
        return type("R", (), {"text": "[]"})()
    client = type("C", (), {"models": type("M", (), {"generate_content": staticmethod(gen)})()})()
    assert agent._generate(client, "p", {}, rounds=2, wait_s=5, sleep=waits.append) == ([], agent.MODELS[0])
    assert waits == [5] and calls == agent.MODELS + agent.MODELS[:1]

    calls.clear(); waits.clear()  # putaran habis -> error asli tetap dilempar
    def always_503(model, **k):
        calls.append(model)
        raise agent.errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})
    client.models.generate_content = always_503
    try:
        agent._generate(client, "p", {}, rounds=2, wait_s=5, sleep=waits.append)
        assert False, "harusnya error"
    except agent.errors.ServerError:
        pass
    assert waits == [5] and len(calls) == 2 * len(agent.MODELS)


def _idea(pillar, source_id, fmt="Feed", style="Perbandingan", hook=None, score=7):
    return {"pillar": pillar, "format": fmt, "hook_style": style, "hook": hook or f"hook {pillar} {source_id}",
            "angle": "a", "why_now": "baru rilis", "key_facts": ["Fakta 1", " ", "Fakta 2"],
            "score": score, "source_id": source_id}


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
    ideas, _ = agent.write(client, picks, datetime.date(2026, 10, 20), [], [])
    assert [(i["pillar"], i["source"]) for i in ideas] == [
        ("Tech & Tutorial", "https://real.com/x"), ("Case Study & Portfolio", "-")]


def test_select_buang_id_tidak_valid():
    items = [{"title": f"t{i}", "link": f"https://s{i}.com/a", "published": "", "summary": ""} for i in range(2)]
    client = _FakeClient([
        {"id": 0, "pillar": "Tech & Tutorial", "why": "ok"},
        {"id": 0, "pillar": "Design & UI/UX", "why": "dobel"},
        {"id": 7, "pillar": "Tech & Tutorial", "why": "out of range"},
        {"id": 1, "pillar": "IT & Tech Industry", "why": "ok"},
    ])
    picks, _ = agent.select(client, items, datetime.date(2026, 10, 27), [])
    assert [(p["link"], p["pillar"]) for p in picks] == [
        ("https://s0.com/a", "Tech & Tutorial"), ("https://s1.com/a", "IT & Tech Industry")]


def test_select_ikut_target_per_pillar():
    items = [{"title": f"t{i}", "link": f"https://s{i}.com/a", "published": "", "summary": ""} for i in range(10)]
    prompts = []
    resp = type("R", (), {"text": json.dumps(
        [{"id": i, "pillar": "Tech & Tutorial", "why": "ok"} for i in range(6)]
        + [{"id": i, "pillar": "Design & UI/UX", "why": "ok"} for i in range(6, 10)])})()
    client = type("C", (), {"models": type("M", (), {
        "generate_content": staticmethod(lambda **k: prompts.append(k["contents"]) or resp)})()})()
    picks, _ = agent.select(client, items, datetime.date(2026, 10, 27), [])
    count = lambda name: sum(p["pillar"] == name for p in picks)
    assert count("Tech & Tutorial") == 5 and count("Design & UI/UX") == 3  # dipotong di batas atas pillar
    assert "Tech & Tutorial (pilih 3-5 sumber)" in prompts[0]


def test_select_batasi_satu_domain_per_hari():
    links = ["https://www.smashingmagazine.com/a", "https://smashingmagazine.com/b",
             "https://dev.to/x", "https://dev.to/y", "https://dev.to/z"]
    items = [{"title": f"t{i}", "link": l, "published": "", "summary": ""} for i, l in enumerate(links)]
    client = _FakeClient([{"id": 0, "pillar": "Design & UI/UX", "why": "ok"},
                          {"id": 1, "pillar": "Design & UI/UX", "why": "domain sama"}]
                         + [{"id": i, "pillar": "Tech & Tutorial", "why": "ok"} for i in (2, 3, 4)])
    picks, _ = agent.select(client, items, datetime.date(2026, 10, 27), [])
    assert [p["link"] for p in picks] == links[0:1] + links[2:4]  # smashing 1x, dev.to 2x


def test_write_rapikan_fakta_dan_skor():
    agent.fetch_article = lambda url: ""
    picks = [{"title": "T", "link": "https://real.com/x", "published": "", "summary": "s",
              "pillar": "Tech & Tutorial"}]
    ideas, _ = agent.write(_FakeClient([_idea("Tech & Tutorial", 1, score=42)]),
                           picks, datetime.date(2026, 10, 20), [], [])
    assert ideas[0]["key_facts"] == ["Fakta 1", "Fakta 2"]  # poin kosong dibuang
    assert ideas[0]["score"] == 10


def test_quality_warnings():
    ideas = [_idea("Tech & Tutorial", 1, hook="Pernah ngerasa deploy lambat?"),
             _idea("Tech & Tutorial", 2), _idea("Tech & Tutorial", 3)]
    w = agent._quality_warnings(ideas)
    assert any("klise" in x for x in w)
    assert any("Perbandingan" in x for x in w)  # gaya sama 3x > maks 2
    assert any("porsi Reels" in x for x in w)   # 0% Reels


def test_render_brief_gpt_dan_history_tetap_kebaca():
    idea = _idea("Tech & Tutorial", 1, fmt="Reels", hook="Tool ini ubah Figma jadi React")
    idea.update(source="https://real.com/x", key_facts=["Export 1 komponen butuh 10 detik"])
    md = agent._render_markdown([idea])
    assert "Prioritas hari ini" in md and "**Kenapa sekarang:** baru rilis" in md
    assert "**Fakta kunci:**\n- Export 1 komponen butuh 10 detik\n" in md
    brief = agent.gpt_brief(idea)
    assert "naskah Reels" in brief and "Hook: Tool ini ubah Figma jadi React" in brief
    assert "- Export 1 komponen butuh 10 detik" in brief and "Sumber: https://real.com/x" in brief
    assert f"```text\n{brief}\n```" in md
    assert "copy carousel" in agent.gpt_brief({**idea, "format": "Feed", "source": "-"})
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "2026-10-26.md"), "w", encoding="utf-8") as f:
            f.write(md)
        links, hooks = agent.load_history(datetime.date(2026, 10, 27), folder=d)
    assert hooks == ["Tool ini ubah Figma jadi React"] and links == {"https://real.com/x"}


def test_load_portfolio_rotasi_per_hari():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "portfolio.json")
        assert agent.load_portfolio(datetime.date(2026, 10, 27), p) is None
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{"name": "A"}, {"name": "B"}], f)
        a = agent.load_portfolio(datetime.date(2026, 10, 27), p)["name"]
        b = agent.load_portfolio(datetime.date(2026, 10, 28), p)["name"]
    assert {a, b} == {"A", "B"}


def test_header_tampilkan_model_dan_tanggal():
    h = agent._header(datetime.date(2026, 10, 27), {"Tanggal": "Selasa, 27 Oktober 2026", "Model": "gemini-3.8-flash"})
    assert h.startswith("# content_2026-10-27\n\n- **Tanggal:** Selasa, 27 Oktober 2026\n- **Model:** gemini-3.8-flash\n")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
