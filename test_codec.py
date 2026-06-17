import websockets
import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("SARVAM_API_KEY")
    async with websockets.connect(
        'wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3', 
        additional_headers={'api-subscription-key': api_key}
    ) as ws:
        await ws.send(json.dumps({'type': 'config', 'data': {'target_language_code': 'en-IN', 'speaker': 'ishita', 'output_audio_codec': 'linear16'}}))
        await ws.send(json.dumps({'type': 'text', 'data': {'text': 'Hello world. This is a test.'}}))
        await ws.send(json.dumps({'type': 'flush'}))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            print(f"Type: {data.get('type')}")
            if data.get("type") == "error":
                print(data)
                break
            if data.get("type") == "event":
                break

asyncio.run(test())
