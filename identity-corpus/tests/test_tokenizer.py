from __future__ import annotations

from pathlib import Path

from identity_corpus.tokenizer import sentence_id, tokenize_sentences


def test_korean_tokenizer_strips_noise_and_thresholds() -> None:
    text = """# 제목
- 짧다
- 이것은 충분히 긴 한국어 문장입니다.
```python
print("코드 문장은 제외됩니다.")
```
[[링크 문서]]를 보면서 저는 제 선택을 다시 설명했어요.
| a | b |
"""

    sentences = tokenize_sentences(text, "kr")

    assert "짧다" not in sentences
    assert all("코드" not in sentence for sentence in sentences)
    assert any("충분히 긴 한국어 문장" in sentence for sentence in sentences)
    assert any("링크 문서" in sentence for sentence in sentences)


def test_english_tokenizer_keeps_sentence_grade_lines() -> None:
    text = """# Heading
- too short.
- This bullet contains enough alphabetic characters to survive.
Another complete sentence appears here! nope.
"""

    sentences = tokenize_sentences(text, "en")

    assert "too short." not in sentences
    assert any(sentence.startswith("This bullet contains") for sentence in sentences)
    assert all(len([c for c in sentence if c.isalpha()]) >= 20 for sentence in sentences)


def test_sentence_id_is_stable_and_path_sensitive(tmp_path: Path) -> None:
    first = sentence_id(" Same text  ", tmp_path / "a.md")
    second = sentence_id("Same   text", tmp_path / "a.md")
    third = sentence_id("Same text", tmp_path / "b.md")

    assert first == second
    assert first != third
