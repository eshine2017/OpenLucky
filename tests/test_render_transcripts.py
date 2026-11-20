from pathlib import Path

from tools.render_transcripts import render_html_to_png


def test_render_html_to_png_produces_nonempty_png(tmp_path: Path) -> None:
    html_file = tmp_path / "transcript.html"
    html_file.write_text(
        "<html><body style='background:#e5ddd5;padding:1rem'>"
        "<p class='msg'>Hello from TestBot</p>"
        "</body></html>",
        encoding="utf-8",
    )
    png = render_html_to_png(html_file)
    assert png == html_file.with_suffix(".png")
    assert png.exists()
    assert png.stat().st_size > 0
