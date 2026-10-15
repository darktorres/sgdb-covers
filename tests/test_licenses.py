"""Tests for tools/licenses.py, which writes the LICENSES.txt that ships with the .exe."""

import unittest

from tools import licenses

LICENSE_RST = """History of the software
=======================

How Python came to be.

Licenses and Acknowledgements for Incorporated Software
=======================================================

OpenSSL
-------

Apache License, Version 2.0
"""


class TestLicenses(unittest.TestCase):
    def build(self, license_rst: str = LICENSE_RST) -> str:
        return licenses.build(own_license="MIT License\n\nCopyright (c) 2026 Rodrigo Torres\n", python="3.14.8",
                              openssl="OpenSSL 3.0.18 30 Sep 2025", python_license="PSF LICENSE AGREEMENT",
                              license_rst=license_rst, pyinstaller="6.22.3",
                              pyinstaller_license="Bootloader Exception")

    def test_covers_everything_in_the_exe_in_order(self) -> None:
        text = self.build()
        parts = ["sgdb-covers.exe is distributed under the MIT License", "Copyright (c) 2026 Rodrigo Torres",
                 "Python 3.14.8 (its LICENSE.txt)", "PSF LICENSE AGREEMENT", INCORPORATED, "Apache License",
                 "PyInstaller 6.22.3 (its bootloader is in the .exe)", "Bootloader Exception"]
        positions = [text.find(part) for part in parts]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("How Python came to be", text)  # only the libraries' part of Doc/license.rst

    def test_a_reorganized_license_page_stops_the_build(self) -> None:
        with self.assertRaises(ValueError):
            self.build("Python's license page, without the libraries' section")

    def test_the_repository_is_mit_licensed(self) -> None:
        text = (licenses.ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertRegex(text, r"(?m)^Copyright \(c\) \d{4} \S.*$")  # what the .exe's Properties show


INCORPORATED = licenses.INCORPORATED

if __name__ == "__main__":
    unittest.main()
