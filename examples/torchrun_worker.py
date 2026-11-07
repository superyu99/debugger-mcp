import os


def main():
    local_rank = int(os.environ.get("LOCAL_RANK", "-1"))
    marker = 100 + local_rank
    print("torchrun worker", local_rank, marker)
    return marker


if __name__ == "__main__":
    main()
