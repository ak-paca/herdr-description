import unicodedata


def description_name(content):
    lines = content.splitlines()
    if not lines or not lines[0].startswith("--"):
        return None
    name = lines[0][2:].strip()
    if not name or any(unicodedata.category(character).startswith("C") for character in name):
        return None
    return name


def meaningful_description(content):
    lines = content.splitlines()
    if lines and lines[0].startswith("--"):
        lines[0] = lines[0][2:]
    return any(line.strip() for line in lines)
