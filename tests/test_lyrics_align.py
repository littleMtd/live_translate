"""Official lyric lines timed by speech-recognition words. The lines are made up, not real lyrics."""
from modules import lyrics_align
from modules.offline_subtitles import group_words

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


def test_misheard_words_get_the_official_text_and_their_timing():
    sung = list(LINES)
    sung[1] = "오늘두 너를 기다료"           # misheard
    cues, stats = lyrics_align.align(_words(sung), LINES, 0, group_words)
    assert [c.text for c in cues] == LINES
    assert stats["lyrics_used"] and stats["lyrics_cues"] == len(LINES) and stats["stt_cues"] == 0
    assert cues[1].start == 1.6 + 1.0 and all(a.end <= b.start for a, b in zip(cues, cues[1:]))


def test_a_repeated_chorus_matches_every_time_and_ad_libs_stay_as_heard():
    sung = [*LINES, "예예", LINES[4], LINES[5]]
    cues, stats = lyrics_align.align(_words(sung), LINES, 0, group_words)
    assert [c.text for c in cues] == [*LINES, "예예", LINES[4], LINES[5]]
    assert stats["stt_cues"] == 1


def test_a_badly_misheard_line_between_two_matched_lines_is_filled_in():
    sung = list(LINES)
    sung[2] = "바라미 부러오며 웃어져"      # 6 of 10 characters wrong: too far off to match on its own
    assert not lyrics_align._matches(lyrics_align._normal(LINES[2]), lyrics_align._normal(sung[2]))
    cues, _ = lyrics_align.align(_words(sung), LINES, 0, group_words)
    assert [c.text for c in cues] == LINES


def test_lyrics_of_another_song_are_not_used():
    heard = ["지금 방송 시작합니다", "오늘은 게임을 할 거예요", "채팅 많이 해주세요"] * 3
    cues, stats = lyrics_align.align(_words(heard), LINES, 0, group_words)
    assert cues is None and stats["lyrics_used"] is False


def test_one_credit_looking_lyric_line_does_not_split_the_block():
    lines = [*LINES[:4], "Stand by my window", *LINES[4:]]
    assert lyrics_align.lyric_lines("\n".join(["작사 : 누군가", "작곡 : 누군가", *lines])) == lines


def test_an_announcement_before_the_lyrics_is_left_out():
    intro = ["새 앨범이 드디어 나왔습니다", "많이 들어주세요!", "감사합니다"]
    assert lyrics_align.lyric_lines("\n".join([*intro, "", *LINES])) == LINES
    assert lyrics_align.lyric_lines("\n".join(intro * 4)) == []


def test_unmatched_words_on_both_sides_of_a_short_line_are_not_merged_across_it():
    words = [*_words(LINES), (100.0, 100.3, "아"), (100.3, 100.6, "창밖에"), (100.6, 100.9, "작은"),
             (100.9, 101.2, "새가"), (101.2, 101.5, "노래해"), (101.6, 101.9, "예")]
    cues, _ = lyrics_align.align(words, LINES, 0, group_words)
    assert [c.text for c in cues[-3:]] == ["아", LINES[0], "예"]
