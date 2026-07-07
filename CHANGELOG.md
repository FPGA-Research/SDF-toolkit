# Changelog

## [0.4.0](https://github.com/FPGA-Research/SDF-toolkit/compare/v0.3.0...v0.4.0) (2026-07-07)


### Features

* PORT-to-INTERCONNECT conversion (+ case-insensitive enum CLI options) ([#9](https://github.com/FPGA-Research/SDF-toolkit/issues/9)) ([405f101](https://github.com/FPGA-Research/SDF-toolkit/commit/405f10199473f5cd282f19e1e8ddea475ef4bc1a))

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
