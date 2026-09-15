def make_braille_graph(data_points: list[float], width: int, height: int = 1) -> str:
    """
    Converts a list of normalized floats (0.0 to 100.0) into a unicode Braille pattern string.

    Args:
        data_points: List of floats representing percentages.
        width: Number of braille characters wide.
        height: Number of braille characters high.

    Returns:
        A string representing the braille graph, with newlines for multiline graphs.
    """
    if not data_points or width <= 0 or height <= 0:
        return ""

    num_cols = width * 2
    max_dots = height * 4

    # Extract the last `num_cols` points or pad with zeros if insufficient
    if len(data_points) >= num_cols:
        points = data_points[-num_cols:]
    else:
        points = [0.0] * (num_cols - len(data_points)) + data_points

    # Pre-calculate dot heights for each column
    dot_heights = [min(max_dots, max(0, int(round((p / 100.0) * max_dots)))) for p in points]

    lines = []
    for r in range(height):
        # Y-coordinate of the bottom of the current character row
        y_base = (height - 1 - r) * 4

        row_chars = []
        for c in range(width):
            x1 = c * 2
            x2 = x1 + 1
            dh1 = dot_heights[x1]
            dh2 = dot_heights[x2]

            char_val = 0x2800  # Base Braille character (empty)

            # y_rel = 3 (Top row of dots)
            if dh1 > y_base + 3: char_val |= 1     # Dot 1
            if dh2 > y_base + 3: char_val |= 8     # Dot 4

            # y_rel = 2 (Second row of dots)
            if dh1 > y_base + 2: char_val |= 2     # Dot 2
            if dh2 > y_base + 2: char_val |= 16    # Dot 5

            # y_rel = 1 (Third row of dots)
            if dh1 > y_base + 1: char_val |= 4     # Dot 3
            if dh2 > y_base + 1: char_val |= 32    # Dot 6

            # y_rel = 0 (Bottom row of dots)
            if dh1 > y_base: char_val |= 64        # Dot 7
            if dh2 > y_base: char_val |= 128       # Dot 8

            row_chars.append(chr(char_val))

        lines.append("".join(row_chars))

    return "\n".join(lines)
