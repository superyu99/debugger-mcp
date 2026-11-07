def helper(value: int) -> int:
    incremented = value + 1
    doubled = incremented * 2
    return doubled


def transform(values: list[int]) -> list[int]:
    scale = 3
    first = helper(values[0])  # Break here, then step into helper.
    result = [first] + [value * scale for value in values[1:]]
    breakpoint_anchor = sum(result)
    return result


if __name__ == "__main__":
    print(transform([1, 2, 3, 4]))
