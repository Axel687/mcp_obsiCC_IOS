from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_mcp import links


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
    files = {
        "Cortex.md": "# Cortex\n\nPoint d'entrée. Voir [[Carl Jung]] et [[philo/Nietzsche|N.]].\n#index\n",
        "Carl Jung.md": "---\ntags: [psychologie, philo/jung]\n---\n# Carl Jung\nArchétypes, voir [[Taoïsme#Wu wei]].\n",
        "philo/Nietzsche.md": "---\ntags: philo/nietzsche, lecture\n---\nRetour à [[Cortex]]. ![[schema.png]]\n",
        "philo/Taoïsme.md": "Le #taoïsme et le [wu wei](../Carl%20Jung.md).\n```\n#pas_un_tag [[Pas un lien]]\n```\n`#non` [[Inexistante]]\n",
        "Isolée.md": "Rien ici. #2024 n'est pas un tag. https://x.fr/#ancre non plus.\n",
        ".obsidian/workspace.md": "[[Cortex]] #cache\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_tags(vault: Path) -> None:
    tags = {row["tag"]: row["count"] for row in links.list_tags()}
    assert tags == {
        "index": 1,
        "psychologie": 1,
        "philo/jung": 1,
        "philo/nietzsche": 1,
        "lecture": 1,
        "taoïsme": 1,
    }
    assert {r["tag"] for r in links.list_tags("philo")} == {"philo/jung", "philo/nietzsche"}
    assert links.notes_with_tag("#philo") == ["Carl Jung.md", "philo/Nietzsche.md"]
    assert links.notes_with_tag("philo", include_nested=False) == []


def test_backlinks_and_resolution(vault: Path) -> None:
    jung = links.backlinks("Carl Jung")
    assert sorted(h["source"] for h in jung["backlinks"]) == ["Cortex.md", "philo/Taoïsme.md"]

    # Bare names resolve across folders; aliases and headings are stripped.
    tao = links.backlinks("philo/Taoïsme")
    assert [h["source"] for h in tao["backlinks"]] == ["Carl Jung.md"]

    cortex = links.backlinks("Cortex.md")
    # The hidden .obsidian folder is ignored.
    assert [h["source"] for h in cortex["backlinks"]] == ["philo/Nietzsche.md"]


def test_outgoing_and_unresolved(vault: Path) -> None:
    out = links.outgoing_links("philo/Taoïsme")
    assert [l["resolved"] for l in out["links"]] == ["Carl Jung.md", None]
    assert out["unresolved"] == ["Inexistante"]  # the link inside the code block is ignored

    nietzsche = links.outgoing_links("philo/Nietzsche")
    embed = [l for l in nietzsche["links"] if l["embed"]][0]
    assert embed["target"] == "schema.png" and embed["resolved"] is None


def test_orphans_and_overview(vault: Path) -> None:
    assert links.find_orphans() == ["Isolée.md"]
    assert links.find_orphans(include_without_outgoing=True) == ["Isolée.md"]

    ov = links.overview()
    assert ov["note_count"] == 5
    assert {"folder": "philo", "notes": 2} in ov["folders"]
    assert ov["unresolved_links"] == [{"target": "Inexistante", "count": 1}]


def test_cache_sees_edits(vault: Path) -> None:
    assert links.notes_with_tag("nouveau") == []
    note = vault / "Isolée.md"
    import os

    note.write_text("#nouveau", encoding="utf-8")
    st = note.stat()
    os.utime(note, (st.st_atime, st.st_mtime + 5))
    assert links.notes_with_tag("nouveau") == ["Isolée.md"]


def test_decomposed_accents_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import unicodedata

    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
    nfd_name = unicodedata.normalize("NFD", "Morale - Bon vs Méchant") + ".md"
    (tmp_path / nfd_name).write_text("x", encoding="utf-8")
    (tmp_path / "Index.md").write_text("[[Morale - Bon vs Méchant]] #réflexion", encoding="utf-8")
    out = links.outgoing_links("Index")
    assert out["unresolved"] == [] and out["links"][0]["resolved"] == nfd_name
    assert [h["source"] for h in links.backlinks(nfd_name)["backlinks"]] == ["Index.md"]
