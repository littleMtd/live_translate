"""Owner-supplied lyric lines timed by forced alignment. The lines are made up, not real lyrics."""
from modules import lyrics_align

LINES = ["창밖에 작은 새가 노래해", "오늘도 너를 기다려", "바람이 불어오면 웃어줘",
         "우리의 여름은 끝나지 않아", "하늘 위로 손을 뻗어 봐", "별빛이 내리는 밤에",
         "조용히 너를 불러", "내일도 같은 자리에서"]


def _words(sung):
    """One word per space-separated token, 0.4 s each, a 1 s pause between sung lines."""
    words, t = [], 0.0
    for line in sung:
        for token in line.split():
            words.append((t, t + 0.4, token))
            t += 0.4
        t += 1.0
    return words


def test_lyric_block_skips_credits_links_and_section_headers():
    description = "\n".join(["랑코 신곡!", "작사 : 누군가", "Composed by someone", "https://x.com/a", "",
                             "[Verse 1]", *LINES[:4], "", "(Chorus)", *LINES[4:], "#MV #노래"])
    assert lyrics_align.lyric_lines(description) == LINES


def test_short_or_prose_descriptions_have_no_lyrics():
    assert lyrics_align.lyric_lines("새 노래 나왔어요\n많이 들어주세요") == []
    assert lyrics_align.lyric_lines("") == []


def test_one_credit_looking_lyric_line_does_not_split_the_block():
    lines = [*LINES[:4], "Stand by my window", *LINES[4:]]
    assert lyrics_align.lyric_lines("\n".join(["작사 : 누군가", "작곡 : 누군가", *lines])) == lines


def test_an_announcement_before_the_lyrics_is_left_out():
    intro = ["새 앨범이 드디어 나왔습니다", "많이 들어주세요!", "감사합니다"]
    assert lyrics_align.lyric_lines("\n".join([*intro, "", *LINES])) == LINES
    assert lyrics_align.lyric_lines("\n".join(intro * 4)) == []


def test_owner_lyrics_drop_blank_lines_and_section_headers():
    assert lyrics_align.clean_lines("[Verse]\n  " + LINES[0] + "  \n\n(Chorus)\n" + LINES[1]) == LINES[:2]


def test_each_line_takes_the_aligner_words_that_spell_it():
    timed = lyrics_align.timed_lines(LINES, _words(LINES), 0)
    assert [t[2] for t in timed] == LINES
    assert timed[1][0] == 1.6 + 1.0                 # four 0.4 s words and a 1 s pause before it
    assert all(a[1] <= b[0] for a, b in zip(timed, timed[1:]))


def test_aligner_words_may_split_or_punctuate_differently():
    words = [(0.0, 0.5, "창밖에"), (0.5, 0.9, "작은새가"), (0.9, 1.4, "노래해,"), (2.0, 2.5, "오늘도"),
             (2.5, 3.0, "너를"), (3.0, 3.2, "기다"), (3.2, 3.5, "려")]
    assert lyrics_align.timed_lines(LINES[:2], words, 0) == [(0.0, 1.4, LINES[0]), (2.0, 3.5, LINES[1])]


def test_words_that_do_not_spell_the_lyrics_are_an_error():
    import pytest

    with pytest.raises(ValueError):
        lyrics_align.timed_lines(LINES[:2], _words(["전혀 다른 말", "또 다른 말"]), 0)


def test_cues_stay_inside_the_video():
    timed = lyrics_align.timed_lines(LINES[:1], [(9.9, 10.5, LINES[0])], 10.0)
    assert timed == [(9.9, 10.0, LINES[0])]


class _Aligner:
    def __init__(self, words, error=None):
        self.words, self.error, self.calls = words, error, []
        self.forced_alignment = self

    def create(self, *, file, text, request_options):
        self.calls.append(text)
        if self.error:
            raise self.error
        from types import SimpleNamespace as N
        return N(loss=0.12345, words=[N(text=w[2], start=w[0], end=w[1], loss=0.1) for w in self.words])


def _encoder(monkeypatch):
    from modules import offline_subtitles

    def fake_run(command, **kwargs):
        open(command[-1], "wb").close()
    monkeypatch.setattr(offline_subtitles.subprocess, "run", fake_run)
    return offline_subtitles


def test_align_lyrics_sends_the_lines_and_builds_cues(monkeypatch):
    offline = _encoder(monkeypatch)
    client = _Aligner(_words(LINES))
    cues, loss = offline.align_lyrics("song.m4a", 60, "ffmpeg", client, LINES, lambda *a, **k: None)
    assert client.calls == ["\n".join(LINES)]
    assert [c.text for c in cues] == LINES and [c.id for c in cues][:2] == ["c1", "c2"]
    assert loss == 0.1235


def test_align_lyrics_explains_a_missing_permission(monkeypatch):
    offline = _encoder(monkeypatch)

    class Denied(Exception):
        status_code = 401
    import pytest

    with pytest.raises(offline.OfflineJobError, match="forced alignment"):
        offline.align_lyrics("song.m4a", 60, "ffmpeg", _Aligner([], Denied()), LINES, lambda *a, **k: None)


def test_align_lyrics_refuses_long_videos(monkeypatch):
    offline = _encoder(monkeypatch)
    import pytest

    with pytest.raises(offline.OfflineSubtitleError):
        offline.align_lyrics("v.m4a", 3 * 3600, "ffmpeg", _Aligner([]), LINES, lambda *a, **k: None)


def test_a_word_merged_across_two_lines_times_both():
    words = [(0.0, 1.0, "창밖에 작은 새가"), (1.0, 2.5, "노래해오늘도"), (2.5, 3.5, "너를 기다려")]
    timed = lyrics_align.timed_lines(LINES[:2], words, 0)
    # the merged word's 1.5 s is split by characters: 3 for the first line, 3 for the second
    assert timed == [(0.0, 1.75, LINES[0]), (1.75, 3.5, LINES[1])]


def test_an_untimed_word_keeps_its_line_in_place():
    words = [(None, None, "창밖에 작은 새가 노래해"), (2.0, 3.5, "오늘도 너를 기다려")]
    assert lyrics_align.timed_lines(LINES[:2], words, 0) == [(2.0, 3.5, LINES[1])]


def test_decomposed_hangul_from_macos_is_kept():
    import unicodedata

    pasted = unicodedata.normalize("NFD", "\n".join(LINES[:2]))
    assert lyrics_align.clean_lines(pasted) == LINES[:2]
