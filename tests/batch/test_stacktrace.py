from redmine_embedding.batch.stacktrace import normalize_log, normalize_trace, split_segments

JAVA = """\
java.lang.NullPointerException: Cannot invoke "String.length()" because "s" is null
\tat com.example.Foo.bar(Foo.java:42)
\tat com.example.Foo.baz(Foo.java:77)
\tat com.example.Main.main(Main.java:10)
Caused by: java.io.IOException: boom
\tat com.example.Io.read(Io.java:5)
\t... 3 more"""

PYTHON = """\
Traceback (most recent call last):
  File "app.py", line 12, in <module>
    main()
  File "app.py", line 8, in main
    raise ValueError("bad")
ValueError: bad"""


def kinds(text):
    return [s.kind for s in split_segments(text)]


def test_java_trace_is_split_from_prose_with_its_header():
    segs = split_segments("エラーが出ました。\n\n" + JAVA + "\n\nどうすればいいですか?")
    assert [s.kind for s in segs] == ["text", "trace", "text"]
    assert segs[1].text.startswith("java.lang.NullPointerException")
    assert "Caused by" in segs[1].text and "3 more" in segs[1].text


def test_python_traceback_including_final_error_line():
    segs = split_segments("before\n" + PYTHON + "\nafter")
    assert kinds("before\n" + PYTHON + "\nafter") == ["text", "trace", "text"]
    assert segs[1].text.rstrip().endswith("ValueError: bad")


def test_log_block_needs_three_timestamped_lines():
    log3 = "\n".join(f"2026-10-26 10:00:0{i} INFO step {i}" for i in range(3))
    log2 = "\n".join(f"2026-10-26 10:00:0{i} INFO step {i}" for i in range(2))
    assert kinds(log3) == ["log"]
    assert kinds(log2) == ["text"]


def test_plain_prose_is_untouched():
    assert kinds("ただの文章です。\n\n二段落目。") == ["text"]


def test_normalize_trace_drops_line_numbers_and_extra_frames():
    out = normalize_trace(JAVA, max_frames=2)
    assert "NullPointerException" in out and "Caused by: java.io.IOException: boom" in out
    assert "42" not in out and ".java" in out  # line numbers gone, file names kept
    assert "Main.main" not in out  # frame 3 dropped


def test_normalize_trace_python_keeps_source_line_without_numbers():
    out = normalize_trace(PYTHON)
    assert "ValueError: bad" in out and 'File "app.py", line' in out
    assert "line 12" not in out and "main()" in out


def test_normalize_log_strips_timestamps_hex_and_uuid():
    text = "2026-10-26T10:00:00.123Z ERROR at 0xdeadbeef id=123e4567-e89b-12d3-a456-426614174000 failed"
    out = normalize_log(text)
    assert out == "ERROR at id= failed"
