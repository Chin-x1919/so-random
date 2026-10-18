
import requests

API_URL = "http://SERVE-IP:8000"
options = ["Pizza", "Sushi", "Burger", "Noodles"]

response = requests.get(
    f"{API_URL}/int",
    params={"min": 0, "max": len(options) - 1},
    timeout=5,
)
response.raise_for_status()

index = response.json()["value"]
print("Selected:", options[index])
  
