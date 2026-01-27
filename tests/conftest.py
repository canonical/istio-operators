#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--charms-path",
        help="Path to directory where charm files are stored.",
    )
    parser.addoption(
        "--cni-bin-dir",
        help="Path to the directory of the CNI binaries.",
    )
    parser.addoption(
        "--cni-conf-dir",
        help="Path to the directory of the CNI configurations.",
    )
