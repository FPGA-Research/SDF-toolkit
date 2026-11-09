# Changelog

## [1.0.0](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.4.1...v1.0.0) (2026-11-09)


### ⚠ BREAKING CHANGES

* split the large modules and trim the docs ([#35](https://github.com/FPGA-Research/SDF-toolkit/issues/35))
* exact Decimal delays and the timing graph queries FABulous needs ([#32](https://github.com/FPGA-Research/SDF-toolkit/issues/32))
* **pathgraph:** topological longest path, scoped interconnects and PORT/DEVICE modes ([#23](https://github.com/FPGA-Research/SDF-toolkit/issues/23))
* **cli:** validate inputs, follow diff(1) exit codes and write SDF without Jinja ([#19](https://github.com/FPGA-Research/SDF-toolkit/issues/19))
* **model:** key entries by arc and read rvalues as transitions ([#18](https://github.com/FPGA-Research/SDF-toolkit/issues/18))

### Features

* **annotate:** add per-sink transport annotation for flat netlists ([#31](https://github.com/FPGA-Research/SDF-toolkit/issues/31)) ([ad7efd2](https://github.com/FPGA-Research/SDF-toolkit/commit/ad7efd2b5d142a71463a36c07d1a867db9c24550))
* exact Decimal delays and the timing graph queries FABulous needs ([#32](https://github.com/FPGA-Research/SDF-toolkit/issues/32)) ([2d16c86](https://github.com/FPGA-Research/SDF-toolkit/commit/2d16c86b35ca9e77bc8c328be0169ae776bf3b2b))
* **parser:** choose OVI SDF 3.0 or IEEE 1497 rules with --standard ([#28](https://github.com/FPGA-Research/SDF-toolkit/issues/28)) ([a1884f4](https://github.com/FPGA-Research/SDF-toolkit/commit/a1884f421aa0a22f9b10083882d0c95ef30aeb01))
* **parser:** read the remaining SDF 3.0 constructs ([#25](https://github.com/FPGA-Research/SDF-toolkit/issues/25)) ([1fd8c18](https://github.com/FPGA-Research/SDF-toolkit/commit/1fd8c18775fd9dc4c3ef49883336d93e87946c61))


### Bug Fixes

* **annotate:** find modules through Yosys JSON and write legal specify blocks ([#21](https://github.com/FPGA-Research/SDF-toolkit/issues/21)) ([e14cc26](https://github.com/FPGA-Research/SDF-toolkit/commit/e14cc268a645e0d51483958d27cb9e6a96690bf5))
* **cli:** validate inputs, follow diff(1) exit codes and write SDF without Jinja ([#19](https://github.com/FPGA-Research/SDF-toolkit/issues/19)) ([0928035](https://github.com/FPGA-Research/SDF-toolkit/commit/09280351b3bc9bb8615533020d070d8bfe2b9479))
* correct interconnect, merge, normalize, validate, diff, query and export ([#20](https://github.com/FPGA-Research/SDF-toolkit/issues/20)) ([8fee437](https://github.com/FPGA-Research/SDF-toolkit/commit/8fee437f7006ae845633ef34ab2477e85fd16e4e))
* derive every input error from SDFToolkitError ([#24](https://github.com/FPGA-Research/SDF-toolkit/issues/24)) ([c3cee46](https://github.com/FPGA-Research/SDF-toolkit/commit/c3cee46b193ee67410eca907d08695223e85969a))
* **parser:** make the grammar conflict-free and raise SDFParseError ([#22](https://github.com/FPGA-Research/SDF-toolkit/issues/22)) ([d18f7d1](https://github.com/FPGA-Research/SDF-toolkit/commit/d18f7d13499710f56da2de019719c8cf31cd3206))
* **pathgraph:** topological longest path, scoped interconnects and PORT/DEVICE modes ([#23](https://github.com/FPGA-Research/SDF-toolkit/issues/23)) ([b016283](https://github.com/FPGA-Research/SDF-toolkit/commit/b0162838eea85882b90b11953493d441fdbee4b3))


### Performance Improvements

* **annotate:** run Yosys beside the SDF parse and hold the collector off ([#30](https://github.com/FPGA-Research/SDF-toolkit/issues/30)) ([9724808](https://github.com/FPGA-Research/SDF-toolkit/commit/9724808c6464984e577525d3e7b6f48146e57331))
* **pathgraph:** keep what each search learns about a frozen graph ([#33](https://github.com/FPGA-Research/SDF-toolkit/issues/33)) ([dfe7ea9](https://github.com/FPGA-Research/SDF-toolkit/commit/dfe7ea916ca39d323076626c3a1adbb316660b16))


### Code Refactoring

* **model:** key entries by arc and read rvalues as transitions ([#18](https://github.com/FPGA-Research/SDF-toolkit/issues/18)) ([d8de9e9](https://github.com/FPGA-Research/SDF-toolkit/commit/d8de9e91fb3c58514ec426b2cd417e2602f8d489))
* split the large modules and trim the docs ([#35](https://github.com/FPGA-Research/SDF-toolkit/issues/35)) ([adecec9](https://github.com/FPGA-Research/SDF-toolkit/commit/adecec94d993aed96b880b6eafc1308bfe97e460))

## [0.4.1](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.4.0...v0.4.1) (2026-11-02)


### Performance Improvements

* **parser:** hold the cyclic collector off during a parse ([30470d1](https://github.com/FPGA-Research/SDF-toolkit/commit/30470d12c55550d5fcfc5a062c3ebe12ba36d23e))
* **parser:** hold the cyclic collector off during a parse ([#15](https://github.com/FPGA-Research/SDF-toolkit/issues/15)) ([000b8ae](https://github.com/FPGA-Research/SDF-toolkit/commit/000b8aededabd2e00d77b40bd5e8253e11960c8c))

## [0.4.0](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.3.0...v0.4.0) (2026-09-17)


### Features

* PORT-to-INTERCONNECT conversion (+ case-insensitive enum CLI options) ([#9](https://github.com/FPGA-Research/SDF-toolkit/issues/9)) ([405f101](https://github.com/FPGA-Research/SDF-toolkit/commit/405f10199473f5cd282f19e1e8ddea475ef4bc1a))


### Bug Fixes

* **parser:** accept single-value rvalues and C-style block comments ([#11](https://github.com/FPGA-Research/SDF-toolkit/issues/11)) ([0f27091](https://github.com/FPGA-Research/SDF-toolkit/commit/0f270918d0fdffa7710bfa78b1e0ba3a5448c3fc))
* **parser:** preserve the original text of COND equations ([#12](https://github.com/FPGA-Research/SDF-toolkit/issues/12)) ([2f8afbd](https://github.com/FPGA-Research/SDF-toolkit/commit/2f8afbd1c7f1b73efffe6cce6da1803a59382338))


### Performance Improvements

* **parser:** embed the transformer and parse cell blocks in parallel ([#13](https://github.com/FPGA-Research/SDF-toolkit/issues/13)) ([c10b792](https://github.com/FPGA-Research/SDF-toolkit/commit/c10b79264094c0908ca6b827c38625be3b1dcd71))

## [0.3.0](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.2.0...v0.3.0) (2026-06-10)


### Features

* **parser:** support PERIOD timing checks ([#7](https://github.com/FPGA-Research/SDF-toolkit/issues/7)) ([0920f9c](https://github.com/FPGA-Research/SDF-toolkit/commit/0920f9cac9b546c77b81ad9f8856d8e6fa80f3b0))

## [0.2.0](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.1.2...v0.2.0) (2026-06-09)


### Features

* **pathgraph:** add traverse_registers option to TimingGraph ([#5](https://github.com/FPGA-Research/SDF-toolkit/issues/5)) ([6b2802a](https://github.com/FPGA-Research/SDF-toolkit/commit/6b2802ae5c5325bdcf8bfee1fabc0ff06ecbb72e))

## [0.1.2](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.1.1...v0.1.2) (2026-06-09)


### Bug Fixes

* correct path computation bugs in pathgraph ([d082545](https://github.com/FPGA-Research/SDF-toolkit/commit/d0825454a16dca1e50b14f94f76afef33bbda0a9))
* **parser:** accept $ in unquoted SDF names ([48a472c](https://github.com/FPGA-Research/SDF-toolkit/commit/48a472c144cf5f39ee459ead877add943e1351e7))

## [0.1.1](https://github.com/KelvinChung2000/SDF-toolkit/compare/v0.1.0...v0.1.1) (2026-02-27)


### Bug Fixes

* regression on python3.11 StrEnum ([9ea036d](https://github.com/KelvinChung2000/SDF-toolkit/commit/9ea036d8683de2e5ba16d5843f5cb0dd6846b8f9))

## 0.1.0 (2026-02-25)


### Features

* more feature added ([946077f](https://github.com/KelvinChung2000/SDF-toolkit/commit/946077fc6977eb9a5eb3eed13bf48e483427efab))
* move to lark ([9d595ff](https://github.com/KelvinChung2000/SDF-toolkit/commit/9d595ffbdca364d80f168c7bd4df655b75c62e21))


### Bug Fixes

* **config:** fix CI, ruff, pre-commit config and remove stale files ([df7f8c5](https://github.com/KelvinChung2000/SDF-toolkit/commit/df7f8c55affaf546c771be6fc46e6acda50cc1f4))
* **emit:** fix None rendering in templates and emit header info ([d3e7711](https://github.com/KelvinChung2000/SDF-toolkit/commit/d3e7711f6e7808489b8e6f48660d1d2c74190cbf))
* fix name type collison ([1122593](https://github.com/KelvinChung2000/SDF-toolkit/commit/11225932da789cbdb2650b7a4b4ce7ccb7fffcae))
* **lint:** add docstrings and fix all ruff lint errors ([5f295be](https://github.com/KelvinChung2000/SDF-toolkit/commit/5f295be171043c605441a714b9eca0a0d7bef4f0))
* **parser:** eliminate transformer state leak and caching inconsistency ([92e8d6b](https://github.com/KelvinChung2000/SDF-toolkit/commit/92e8d6bfe858b3aa8fefbb952bd7bf0b453c388a))
* relocate __main__.py, add comment support, public API, and docstring ([296ac11](https://github.com/KelvinChung2000/SDF-toolkit/commit/296ac11add8d554062f7a8b93c3cf3dd54b0a151))


### Documentation

* Improve the README further. ([32ddbc8](https://github.com/KelvinChung2000/SDF-toolkit/commit/32ddbc8aacd2113153afa5e865d2472c17dca639))
