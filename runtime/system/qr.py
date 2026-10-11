"""A QR code, made here so the install shows the 6-digit code's secret with no network and no library to scan.

It follows ISO/IEC 18004 for what this needs: byte mode, error correction level M, the smallest of versions 1 to 10
that holds the text (up to 213 bytes), and the mask with the lowest penalty. The console draws its dark modules from
``svg_path``; the terminal prints them as half blocks.
"""

from __future__ import annotations

from collections.abc import Callable

# Level M, versions 1 to 10: error-correction codewords in each block, and the number of blocks.
_ECC_PER_BLOCK = (10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_BLOCKS = (1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_LEVEL_M = 0b00  # the format information's two bits for level M
QUIET = 4  # the light border every reader needs, in modules

_MASKS: tuple[Callable[[int, int], bool], ...] = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _gf_multiply(x: int, y: int) -> int:
    """Multiplication in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_multiply(coefficient, factor)
    return result


def _raw_modules(version: int) -> int:
    """The modules left for data and error correction once the function patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        aligns = version // 7 + 2
        result -= (25 * aligns - 10) * aligns - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[version - 1] * _BLOCKS[version - 1]


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    aligns = version // 7 + 2
    step = (version * 8 + aligns * 3 + 5) // (aligns * 4 - 4) * 2
    size = version * 4 + 17
    return sorted([size - 7 - i * step for i in range(aligns - 1)] + [6])


def _codewords(data: bytes, version: int) -> list[int]:
    """The data in byte mode, padded to the version's capacity, then split into blocks, each with its error
    correction, and interleaved."""
    count_bits = 8 if version < 10 else 16
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    bits += [(byte >> i) & 1 for byte in data for i in reversed(range(8))]
    capacity = _data_codewords(version) * 8
    bits += [0] * min(4, capacity - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, ecc_len = _BLOCKS[version - 1], _ECC_PER_BLOCK[version - 1]
    raw = _raw_modules(version) // 8
    short_count, short_len = blocks - raw % blocks, raw // blocks
    divisor = _divisor(ecc_len)
    split: list[list[int]] = []
    start = 0
    for i in range(blocks):
        size = short_len - ecc_len + (0 if i < short_count else 1)
        part = words[start : start + size]
        start += size
        ecc = _remainder(part, divisor)
        split.append(part + ([0] if i < short_count else []) + ecc)
    # A short block's placeholder byte, at the column where the long blocks hold one more data codeword, is skipped.
    return [block[i] for i in range(len(split[0])) for j, block in enumerate(split)
            if i != short_len - ecc_len or j >= short_count]  # fmt: skip


class _Grid:
    def __init__(self, version: int) -> None:
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]
        for i in range(self.size):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (self.size - 4, 3), (3, self.size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= x + dx < self.size and 0 <= y + dy < self.size:
                        self.put(x + dx, y + dy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(version)
        last = len(positions) - 1
        for i, x in enumerate(positions):
            for j, y in enumerate(positions):
                if (i, j) not in ((0, 0), (0, last), (last, 0)):
                    for dy in range(-2, 3):
                        for dx in range(-2, 3):
                            self.put(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)
        if version >= 7:
            remainder = version
            for _ in range(12):
                remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
            bits = version << 12 | remainder
            for i in range(18):
                a, b, dark = self.size - 11 + i % 3, i // 3, (bits >> i) & 1 == 1
                self.put(a, b, dark)
                self.put(b, a, dark)

    def put(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def format_bits(self, mask: int) -> None:
        data = _LEVEL_M << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):
            self.put(8, i, bit[i])
        self.put(8, 7, bit[6])
        self.put(8, 8, bit[7])
        self.put(7, 8, bit[8])
        for i in range(9, 15):
            self.put(14 - i, 8, bit[i])
        for i in range(8):
            self.put(self.size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.put(8, self.size - 15 + i, bit[i])
        self.put(8, self.size - 8, True)

    def place(self, words: list[int]) -> None:
        """The codewords in the standard's zigzag, two columns at a time from the right, skipping the timing column."""
        i, total = 0, len(words) * 8
        for right in range(self.size - 1, 0, -2):
            column = right - 1 if right <= 6 else right
            upward = (column + 1) & 2 == 0
            for vertical in range(self.size):
                y = self.size - 1 - vertical if upward else vertical
                for x in (column, column - 1):
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1

    def masked(self, mask: int) -> list[list[bool]]:
        self.format_bits(mask)
        rule = _MASKS[mask]
        return [[self.dark[y][x] ^ (not self.fixed[y][x] and rule(x, y)) for x in range(self.size)]
                for y in range(self.size)]  # fmt: skip


def _penalty(grid: list[list[bool]]) -> int:
    size = len(grid)
    lines = grid + [list(column) for column in zip(*grid, strict=True)]
    score = 0
    for line in lines:
        run = 1
        for a, b in zip(line, line[1:], strict=False):
            if a == b:
                run += 1
                continue
            score += run - 2 if run >= 5 else 0
            run = 1
        score += run - 2 if run >= 5 else 0
        bits = "".join("1" if dark else "0" for dark in line)
        score += 40 * (bits.count("10111010000") + bits.count("00001011101"))
    for y in range(size - 1):
        for x in range(size - 1):
            if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3
    dark = sum(row.count(True) for row in grid)
    return score + 10 * (abs(dark * 20 - size * size * 10) // (size * size))


def matrix(text: str, *, mask: int | None = None) -> list[list[bool]]:
    """``text``'s QR code, row by row, True for a dark module; the quiet border is the caller's to add."""
    data = text.encode("utf-8")
    version = next((v for v in range(1, len(_BLOCKS) + 1) if 4 + (8 if v < 10 else 16) + 8 * len(data)
                    <= _data_codewords(v) * 8), None)  # fmt: skip
    if version is None:
        raise ValueError(f"{len(data)} bytes are more than a QR code of version 10 holds at level M")
    grid = _Grid(version)
    grid.place(_codewords(data, version))
    if mask is not None:
        return grid.masked(mask)
    return min((grid.masked(each) for each in range(len(_MASKS))), key=_penalty)


def svg_path(grid: list[list[bool]]) -> str:
    """Every dark module as one SVG path, a row's runs merged, inside a quiet border of ``QUIET`` modules."""
    parts = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            parts.append(f"M{start + QUIET} {y + QUIET}h{x - start}v1h-{x - start}z")
    return "".join(parts)


def half_blocks(grid: list[list[bool]]) -> list[str]:
    """The code as terminal lines, two rows to a line, dark on a light ground so it scans on any terminal theme."""
    size = len(grid) + 2 * QUIET
    light = [False] * size
    rows = [light] * QUIET + [light[:QUIET] + row + light[:QUIET] for row in grid] + [light] * (QUIET + 1)
    glyph = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    return ["\x1b[30;107m" + "".join(glyph[(rows[y][x], rows[y + 1][x])] for x in range(size)) + "\x1b[0m"
            for y in range(0, size, 2)]  # fmt: skip
