import requests


def run_test():
    query = "What exact mathematical formula defines Scaled Dot-Product Attention in the Transformer paper?"
    url = "http://127.0.0.1:8004/query"
    payload = {"query": query}
    resp = requests.post(url, json=payload).json()

    print("Response citations:")
    for c in resp.get("citations", []):
        print(c)
        assert isinstance(c, dict), "Citation is not a dict!"
        assert "error" not in c, f"Citation has error: {c}"
        assert (
            "location_reference" in c or "bounding_box" in c
        ), "Missing grounding info!"

    print("ALL OK")


if __name__ == "__main__":
    run_test()
