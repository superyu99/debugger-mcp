def worker():
    rank = 0
    payload = [3, 5, 8]
    marker = sum(payload) + rank
    print("child marker", marker)
    return marker


if __name__ == "__main__":
    worker()
