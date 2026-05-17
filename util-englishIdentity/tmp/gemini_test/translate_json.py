import json

input_json_string = """[{"id":"abc1","kr":"안녕하세요","folder":"daily"}]"""

# Step 1: Parse Input JSON
input_data = json.loads(input_json_string)

output_data = []

# Step 2 & 3: Translate Korean ('kr') to English ('en') and Construct Output JSON Objects
for item in input_data:
    item_id = item['id']
    korean_text = item['kr']

    # Simulate translation
    if korean_text == "안녕하세요":
        english_text = "Hello"
    else:
        english_text = "Translation placeholder" # Fallback for other cases

    output_data.append({"id": item_id, "en": english_text})

# Step 4: Serialize Output to JSON String
output_json_string = json.dumps(output_data)

print(output_json_string)
