"""
api.providers.languages -- the language list behind /paste's `language`
autocomplete (see commands/url.py's `_language_autocomplete`).

**This is a suggestion list, not a validated one.** Every paste provider
in this package treats `language` as an unvalidated passthrough string
(see e.g. api.providers.ez_host.create_paste's own docstring: "language
isn't validated against a known list here... its own response is still
the final word on an invalid value") -- and each provider's own accepted
identifiers differ in spelling in places (e.g. pastee.dev's own /v1/syntaxes
endpoint is the actual source of truth for what pastee.dev accepts, not
this list). Discord's autocomplete mechanism fits that reality well:
unlike `app_commands.choices()`, it only *suggests* -- the person can
still type any string that isn't in LANGUAGES below and it's sent through
exactly as typed, so this list being incomplete or using a spelling one
specific provider doesn't recognize is never a hard failure, just a missed
suggestion. Provider-specific rejection of a genuinely bad value still
surfaces as that provider's own error, same as ever.

LANGUAGES is ordered, not alphabetized-then-left -- the first entries are
"plaintext"/"autodetect" (recognized as the no-highlighting sentinel by
at least one provider each -- see pastee_dev.py's own docstring)
followed by the languages this bot's own domain (Discord
bots, Roblox/Luau scripting) makes most likely to actually get pasted,
then everything else alphabetically. search_languages() below preserves
that ordering for an empty query so opening the autocomplete box with
nothing typed yet surfaces the most relevant handful first rather than
whatever sorts first alphabetically.
"""

from typing import List, Tuple

# (display name, value) -- value is what's actually sent as `language` to
# whichever provider's create_paste(). Kept lowercase/hyphen-free where
# there's a common convention across syntax highlighters, but see this
# module's docstring: these are suggestions, not a validated contract.
_PRIORITY: List[Tuple[str, str]] = [
    ("Plain Text", "plaintext"),
    ("Auto Detect", "autodetect"),
    ("Python", "python"),
    ("Lua", "lua"),
    ("Luau", "luau"),
    ("JavaScript", "javascript"),
    ("TypeScript", "typescript"),
    ("Java", "java"),
    ("C", "c"),
    ("C++", "cpp"),
    ("C#", "csharp"),
    ("Go", "go"),
    ("Rust", "rust"),
    ("HTML", "html"),
    ("CSS", "css"),
    ("JSON", "json"),
    ("YAML", "yaml"),
    ("Bash", "bash"),
    ("SQL", "sql"),
    ("Markdown", "markdown"),
]

_OTHER: List[Tuple[str, str]] = sorted([
    ("ABAP", "abap"),
    ("Ada", "ada"),
    ("Agda", "agda"),
    ("Ansible", "ansible"),
    ("Apache Config", "apache"),
    ("Apex", "apex"),
    ("AppleScript", "applescript"),
    ("ActionScript", "actionscript"),
    ("Assembly (x86)", "asm"),
    ("AutoHotkey", "autohotkey"),
    ("AutoIt", "autoit"),
    ("AWK", "awk"),
    ("Batch", "batch"),
    ("Befunge", "befunge"),
    ("Brainfuck", "brainfuck"),
    ("C++ (Header)", "hpp"),
    ("Clojure", "clojure"),
    ("CMake", "cmake"),
    ("COBOL", "cobol"),
    ("CoffeeScript", "coffeescript"),
    ("Common Lisp", "lisp"),
    ("CSV", "csv"),
    ("CUDA", "cuda"),
    ("Crystal", "crystal"),
    ("D", "d"),
    ("Dart", "dart"),
    ("Delphi", "delphi"),
    ("Diff / Patch", "diff"),
    ("Docker Compose", "docker-compose"),
    ("Dockerfile", "dockerfile"),
    ("Elixir", "elixir"),
    ("Elm", "elm"),
    ("Emacs Lisp", "elisp"),
    ("Erlang", "erlang"),
    ("F#", "fsharp"),
    ("Factor", "factor"),
    ("Fish", "fish"),
    ("Forth", "forth"),
    ("Fortran", "fortran"),
    ("Gherkin (Cucumber)", "gherkin"),
    ("GLSL", "glsl"),
    ("Gradle", "gradle"),
    ("GraphQL", "graphql"),
    ("Groovy", "groovy"),
    ("Handlebars", "handlebars"),
    ("Haskell", "haskell"),
    ("HCL (Terraform)", "hcl"),
    ("HLSL", "hlsl"),
    ("Idris", "idris"),
    ("INI", "ini"),
    ("Io", "io"),
    ("Jinja2", "jinja2"),
    ("JScript", "jscript"),
    ("JSON5", "json5"),
    ("JSX", "jsx"),
    ("Julia", "julia"),
    ("Kotlin", "kotlin"),
    ("LaTeX", "latex"),
    ("Less", "less"),
    ("Logo", "logo"),
    ("Makefile", "makefile"),
    ("MATLAB", "matlab"),
    ("Nginx Config", "nginx"),
    ("Nim", "nim"),
    ("Nix", "nix"),
    ("Objective-C", "objectivec"),
    ("Objective-C++", "objectivecpp"),
    ("OCaml", "ocaml"),
    ("Pascal", "pascal"),
    ("Perl", "perl"),
    ("PHP", "php"),
    ("Pike", "pike"),
    ("PL/SQL", "plsql"),
    ("PowerShell", "powershell"),
    ("Prolog", "prolog"),
    ("Protocol Buffers", "protobuf"),
    ("Puppet", "puppet"),
    ("PureScript", "purescript"),
    ("Q#", "qsharp"),
    ("R", "r"),
    ("Racket", "racket"),
    ("Reason ML", "reason"),
    ("Red", "red"),
    ("Regex", "regex"),
    ("ReScript", "rescript"),
    ("reStructuredText", "rst"),
    ("Ruby", "ruby"),
    ("SAS", "sas"),
    ("Scala", "scala"),
    ("Scheme", "scheme"),
    ("SCSS", "scss"),
    ("Sass", "sass"),
    ("Scratch", "scratch"),
    ("Smalltalk", "smalltalk"),
    ("Solidity", "solidity"),
    ("Stata", "stata"),
    ("Svelte", "svelte"),
    ("Swift", "swift"),
    ("Tcl", "tcl"),
    ("Terraform", "terraform"),
    ("Toml", "toml"),
    ("TSX", "tsx"),
    ("T-SQL", "tsql"),
    ("Turing", "turing"),
    ("Vala", "vala"),
    ("VB.NET", "vbnet"),
    ("Verilog", "verilog"),
    ("VHDL", "vhdl"),
    ("Vim Script", "vim"),
    ("Visual Basic", "vb"),
    ("Visual FoxPro", "foxpro"),
    ("Vue", "vue"),
    ("WebAssembly", "wasm"),
    ("Wolfram / Mathematica", "mathematica"),
    ("XML", "xml"),
    ("Zig", "zig"),
    ("Zsh", "zsh"),
], key=lambda pair: pair[0].lower())

LANGUAGES: List[Tuple[str, str]] = _PRIORITY + _OTHER

# Discord's own hard ceiling on the number of autocomplete results a
# response can carry.
MAX_AUTOCOMPLETE_RESULTS = 25


def search_languages(query: str, limit: int = MAX_AUTOCOMPLETE_RESULTS) -> List[Tuple[str, str]]:
    """Filters LANGUAGES for `query` (case-insensitive) -- built for
    commands/url.py's `_language_autocomplete`.

    Empty `query` returns LANGUAGES' own priority-first ordering (see this
    module's docstring) truncated to `limit`, rather than an alphabetical
    slice, so opening the autocomplete box with nothing typed surfaces the
    most likely-relevant languages first.

    Non-empty `query` ranks a name/value that *starts with* it above one
    that merely *contains* it (e.g. searching "script" surfaces
    "JavaScript"/"TypeScript" before "CoffeeScript" only because they're
    alphabetically earlier -- but searching "java" correctly puts "Java"
    itself ahead of "JavaScript"), checked against both the display name
    and the value since someone might type either ("c++" or "cpp").
    Relative order within each rank follows LANGUAGES' own ordering.
    """
    if not query:
        return LANGUAGES[:limit]

    q = query.lower()
    starts: List[Tuple[str, str]] = []
    contains: List[Tuple[str, str]] = []
    for name, value in LANGUAGES:
        name_l, value_l = name.lower(), value.lower()
        if name_l.startswith(q) or value_l.startswith(q):
            starts.append((name, value))
        elif q in name_l or q in value_l:
            contains.append((name, value))

    # Within the "starts with" tier, shorter/more exact matches first --
    # otherwise list order alone would put e.g. "JavaScript" ahead of
    # "Java" for the query "java", just because JavaScript happens to sit
    # earlier in LANGUAGES' own priority ordering. Stable sort preserves
    # LANGUAGES' relative order among same-length ties.
    starts.sort(key=lambda pair: min(len(pair[0]), len(pair[1])))

    return (starts + contains)[:limit]


# Filename (no extension, lowercased) -> language value, for the handful
# of conventional source filenames that don't carry an extension at all.
# Checked before _EXTENSION_LANGUAGE_MAP below by language_for_filename().
_SPECIAL_FILENAME_LANGUAGES = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "vagrantfile": "ruby",
}

# Extension (no leading dot, lowercased) -> language value, for
# /paste's `file1`-`file4` extra-file attachments (see commands/url.py's
# `_url_paste_impl`) -- each of those slots has nowhere left to type a
# `language` explicitly (file5_title/file5_language and their file2-4
# siblings were removed in favor of one attachment per slot), so the
# language is inferred from the attachment's filename instead.
#
# Same "suggestion, not a validated contract" caveat as LANGUAGES itself
# (this module's own docstring) applies doubly here: an extension with no
# entry below -- or one this bot doesn't recognize -- falls back to
# "plaintext" via language_for_filename() rather than failing the paste,
# and whichever provider ultimately receives it is still the final word
# on whether it likes the value.
_EXTENSION_LANGUAGE_MAP = {
    "py": "python", "pyw": "python", "pyi": "python",
    "lua": "lua",
    "luau": "luau",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "jsx",
    "ts": "typescript", "mts": "typescript", "cts": "typescript",
    "tsx": "tsx",
    "java": "java",
    "c": "c",
    "h": "c",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "c++": "cpp",
    "hpp": "hpp", "hh": "hpp", "hxx": "hpp",
    "cs": "csharp",
    "go": "go",
    "rs": "rust",
    "html": "html", "htm": "html",
    "css": "css",
    "scss": "scss",
    "sass": "sass",
    "less": "less",
    "json": "json",
    "json5": "json5",
    "jsonc": "json",
    "yaml": "yaml", "yml": "yaml",
    "sh": "bash", "bash": "bash",
    "zsh": "zsh",
    "fish": "fish",
    "ps1": "powershell", "psm1": "powershell", "psd1": "powershell",
    "bat": "batch", "cmd": "batch",
    "sql": "sql",
    "md": "markdown", "markdown": "markdown",
    "rst": "rst",
    "xml": "xml",
    "toml": "toml",
    "ini": "ini", "cfg": "ini", "conf": "ini",
    "cmake": "cmake",
    "gradle": "gradle",
    "kt": "kotlin", "kts": "kotlin",
    "swift": "swift",
    "m": "objectivec",
    "mm": "objectivecpp",
    "php": "php",
    "rb": "ruby",
    "pl": "perl", "pm": "perl",
    "r": "r",
    "jl": "julia",
    "dart": "dart",
    "ex": "elixir", "exs": "elixir",
    "erl": "erlang",
    "hs": "haskell",
    "clj": "clojure", "cljs": "clojure", "cljc": "clojure",
    "scala": "scala",
    "groovy": "groovy",
    "vb": "vb", "vbs": "vb",
    "asm": "asm", "s": "asm",
    "proto": "protobuf",
    "graphql": "graphql", "gql": "graphql",
    "vue": "vue",
    "svelte": "svelte",
    "nim": "nim",
    "zig": "zig",
    "d": "d",
    "pas": "pascal", "pp": "pascal",
    "adb": "ada", "ads": "ada",
    "f90": "fortran", "f": "fortran",
    "tf": "terraform",
    "hcl": "hcl",
    "diff": "diff", "patch": "diff",
    "csv": "csv",
    "tex": "latex",
    "sol": "solidity",
    "elm": "elm",
    "coffee": "coffeescript",
    "apex": "apex", "cls": "apex",
    "awk": "awk",
    "txt": "plaintext", "log": "plaintext",
}


def language_for_filename(filename: str) -> str:
    """Guesses a `language` value for /paste's `file1`-`file4` extra-file
    attachments (see commands/url.py's `_url_paste_impl`) from `filename`
    alone -- there's no separate language option for those slots anymore
    (see _EXTENSION_LANGUAGE_MAP's own docstring for why), so this is the
    only signal available.

    Checks _SPECIAL_FILENAME_LANGUAGES first (for the handful of
    conventional filenames -- "Dockerfile", "Makefile", etc. -- that
    carry no extension at all), then falls back to
    _EXTENSION_LANGUAGE_MAP keyed on whatever follows the last `.` in
    `filename`. "plaintext" (LANGUAGES' own no-highlighting sentinel --
    see this module's docstring) covers both a filename with no
    recognized extension and one with no extension at all.
    """
    base = (filename or "").strip()
    # Discord attachment filenames are just a name, never a path -- the
    # separator handling here is defensive only, in case that ever isn't
    # true for some client/platform.
    base = base.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    lower = base.lower()

    if lower in _SPECIAL_FILENAME_LANGUAGES:
        return _SPECIAL_FILENAME_LANGUAGES[lower]

    if "." in base:
        ext = base.rsplit(".", 1)[-1].lower()
        return _EXTENSION_LANGUAGE_MAP.get(ext, "plaintext")

    return "plaintext"