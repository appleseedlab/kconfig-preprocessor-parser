# kconfig-preprocessor-parser

Extract C preprocessor conditional blocks (`#ifdef` / `#if` / `#elif` / `#else`)
from source files using [tree-sitter](https://tree-sitter.github.io/), and
resolve each branch to the **effective condition** that must hold for its lines
to compile.

Built for Linux kernel configuration analysis, where knowing *which* `CONFIG_*`
symbols guard a given line matters. Scope is deliberately narrow: only
`CONFIG_*` symbols are resolved (see [Limitations](#limitations)).

## Install

```bash
uv add kconfig-preprocessor-parser
```

Requires Python 3.10+.

## Quick start

Given `s.c`:

```c
int fn(void) {
#ifdef CONFIG_A
a();
#elif defined(CONFIG_B)
b();
#else
c();
#endif
return 0;
}
```

Each branch resolves to the full condition, including the negated preceding
branches:

```python
from pathlib import Path
from kconfig_preprocessor_parser import extract_preproc_branch_blocks_from_file

for block, cond in extract_preproc_branch_blocks_from_file(
    kernel_src=Path("."), c_file_path=Path("s.c")
):
    print(block.type, block.body_lines, "->", cond.raw_expression)
```

```
preproc_ifdef [3] -> CONFIG_A
preproc_elif  [5] -> not CONFIG_A and CONFIG_B
preproc_else  [7] -> not CONFIG_A and not CONFIG_B
```

Note `raw_expression` for the `#elif` and `#else` branches: the parser carries
the negation of every earlier branch in the chain, so the expression is the
complete guard for those lines, not just the local directive.

Nested blocks compose the same way — a `#ifdef CONFIG_B` inside a
`#ifdef CONFIG_A` yields `CONFIG_A and CONFIG_B`.

Boolean operators are supported: `defined(A) && defined(B)` resolves to
`((A) and (B))`, and `||` resolves to `or`.

## Limitations

**Value comparisons are not resolved.** Conditions are reduced in terms of
whether a symbol is defined, so any comparison against a value fails:

```
#if LINUX_VERSION_CODE > 100   ->  parseable=False, "unsupported_condition_expression"
#if CONFIG_NR_CPUS > 4         ->  parseable=False, "unsupported_condition_expression"
```

Unresolved conditions are never silently dropped. They come back with
`parseable=False`, a `failure_reason`, and the original text in
`raw_expression`, so you can filter or handle them yourself.

**Only `CONFIG_*` symbols are resolved.** Any other identifier is reported with
`parseable=False` and `failure_reason="non_config_symbol"`:

```
#ifdef CONFIG_A     ->  parseable=True
#ifdef __KERNEL__   ->  parseable=False, "non_config_symbol"
#ifdef DEBUG        ->  parseable=False, "non_config_symbol"
```

**Conditions are read syntactically from one file.** Macros are not expanded and
`#include` is not followed, so `#define MY_FLAG CONFIG_A` followed by
`#ifdef MY_FLAG` reports `MY_FLAG`, not `CONFIG_A`. Symbols are reported as
guards regardless of whether they are actually defined — resolve them against a
real config with `parse_enabled_configs`.

**C only.** Backed by `tree-sitter-c`; C++ is not supported.

## Coverage reports

`get_all_lines_in_preproc` and
`get_all_parseable_preproc_lines_in_covered_functions` read syzkaller coverage
JSON. Reports record absolute paths from the machine the kernel was built on,
which rarely match the tree you are analyzing, so paths are mapped onto
`kernel_src` by longest matching suffix:

```python
get_all_parseable_preproc_lines_in_covered_functions(
    coverage_file=Path("coverage.json"),
    kernel_src=Path("/src/linux"),
)
```

Pass `strip_prefix` to map them explicitly instead:

```python
get_all_parseable_preproc_lines_in_covered_functions(
    coverage_file=Path("coverage.json"),
    kernel_src=Path("/src/linux"),
    strip_prefix="/build/ci/kernel-6.1",
)
```

Files that cannot be mapped onto `kernel_src` are skipped, not fatal.

## License

MIT
