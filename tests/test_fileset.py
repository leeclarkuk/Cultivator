from cultivator.fileset import FileHasher, FileSet


def touch(base, *rels):
    for rel in rels:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)


def names(fs, base):
    return [p.relative_to(base).as_posix() for p in fs.files()]


def test_recursive_and_extension_patterns(tmp_path):
    touch(tmp_path, "a.py", "src/b.py", "src/deep/c.py", "src/d.md", "other/e.py")
    assert names(FileSet(tmp_path, ["src/**"]), tmp_path) == ["src/b.py", "src/d.md", "src/deep/c.py"]
    assert names(FileSet(tmp_path, ["**/*.py"]), tmp_path) == ["a.py", "other/e.py", "src/b.py", "src/deep/c.py"]


def test_directory_pattern_covers_contents(tmp_path):
    touch(tmp_path, "src/b.py", "src/deep/c.py", "x.py")
    assert names(FileSet(tmp_path, ["src"]), tmp_path) == ["src/b.py", "src/deep/c.py"]


def test_ignore_applies_to_files_and_directories(tmp_path):
    touch(tmp_path, "a.py", "docs/readme.md", "notes.md", "build/out.bin")
    fs = FileSet(tmp_path, ["**"], ["**/*.md", "build"])
    assert names(fs, tmp_path) == ["a.py"]


def test_always_excluded(tmp_path):
    touch(tmp_path, "a.py", ".git/config", "__pycache__/x.pyc", ".cultivator/state/dev.json")
    assert names(FileSet(tmp_path, ["**"]), tmp_path) == ["a.py"]


def test_pattern_reaching_outside_base(tmp_path):
    base = tmp_path / "mod"
    base.mkdir()
    touch(tmp_path, "shared/lib.py", "shared/more/x.py", "mod/own.py")
    fs = FileSet(base, ["**", "../shared/**"])
    assert names(fs, tmp_path) == ["mod/own.py", "shared/lib.py", "shared/more/x.py"]
    assert fs.includes(tmp_path / "shared" / "lib.py")
    assert not fs.includes(tmp_path / "elsewhere.py")


def test_includes_handles_deleted_paths(tmp_path):
    fs = FileSet(tmp_path, ["src/**"])
    assert fs.includes(tmp_path / "src" / "gone.py")
    assert not fs.includes(tmp_path / "docs" / "gone.py")


def test_hasher_detects_content_change_and_uses_cache(tmp_path):
    import os
    f = tmp_path / "f.txt"
    f.write_text("one")
    old = f.stat().st_mtime - 3600
    os.utime(f, (old, old))
    hasher = FileHasher()
    first = hasher.digest(f)
    assert hasher.digest(f) == first and hasher.hits == 1
    f.write_text("three")
    os.utime(f, (old, old))  # same mtime, different size
    assert hasher.digest(f) != first


def test_recent_files_are_not_cached(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one")
    hasher = FileHasher()
    hasher.digest(f)
    f.write_text("two")  # same size and possibly the same mtime tick
    assert hasher.digest(f) != "7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed"
    assert hasher.cache == {}
