import base64
import json
import os
from flask import Flask, request
from flask_sock import Sock
from flask_cors import CORS
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
from dotenv import load_dotenv
from google.cloud import speech
import csv
from datetime import datetime
import wave
import audioop  

load_dotenv()

app = Flask(__name__)
CORS(app)
sock = Sock(app)
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
NGROK_URL = os.getenv('NGROK_URL')
if not NGROK_URL:
    raise ValueError("NGROK_URL environment variable is not set")

NGROK_HOST = NGROK_URL.replace('https://', '').replace('http://', '')

speech_client = speech.SpeechClient()

config = speech.RecognitionConfig(
    encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
    sample_rate_hertz=8000,
    language_code="en-US",
    enable_automatic_punctuation=True,
)

streaming_config = speech.StreamingRecognitionConfig(
    config=config,
    interim_results=True
)

@app.route('/make-call', methods=['POST'])
def make_call():
    data = request.get_json()
    if not data or 'phone_number' not in data or 'name' not in data or 'email' not in data:
        return {'error': 'Missing data'}, 400

    phone_number = data['phone_number'].strip()
    name = data['name'].strip()
    email = data['email'].strip()

    if not phone_number.startswith('+1'):
        phone_number = '+1' + phone_number.lstrip('+1')

    if not phone_number.replace('+1', '').isdigit() or len(phone_number) != 12:
        return {'error': 'Invalid phone number format'}, 400

    if os.path.exists('recordings'):
        for file in os.listdir('recordings'):
            file_path = os.path.join('recordings', file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")
    
    call = twilio_client.calls.create(
        to=phone_number,
        from_=os.getenv('TWILIO_PHONE_NUMBER'),
        url=f"{NGROK_URL}/start-stream",
        status_callback=f"{NGROK_URL}/call-status",
        status_callback_event=['completed', 'answered', 'failed']
    )
    print(f'Started outgoing call: {call.sid}')
    return {'call_sid': call.sid}, 200

@app.route('/call-status', methods=['POST'])
def call_status():
    """Handle call status updates."""
    status = request.values.get('CallStatus', '')
    call_sid = request.values.get('CallSid', '')
    print(f"Call {call_sid} status: {status}")
    return '', 200

@app.route('/start-stream', methods=['POST'])
def start_stream():
    #stream setup
    response = VoiceResponse()
    response.say('Please start speaking.')
    response.pause(length=1) 
    start = Start()
    start.stream(url=f'wss://{NGROK_HOST}/stream')
    response.append(start)
    
    # First question after 10 seconds
    #response.pause(length=5)
    #response.say("Test 1")
    
    # Second question after another 10 seconds
    #response.pause(length=5)
    #response.say("Test 2")
    
    # Third question after another 10 seconds
    #response.pause(length=5)
    #response.say("Test 3")
    
    # Final pause to allow for answer to last question
    response.pause(length=5)
    
    return str(response), 200, {'Content-Type': 'text/xml'}

@sock.route('/stream')
def stream(ws):
    """Receive and transcribe audio stream."""
    print("WebSocket connection opened")
    
    call_sid = request.args.get('CallSid', 'unknown')
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    audio_filename = f'recordings/{timestamp}_{call_sid}.wav'
    os.makedirs('recordings', exist_ok=True)
    

    wav_file = wave.open(audio_filename, 'wb')
    wav_file.setnchannels(1)  
    wav_file.setsampwidth(2)  
    wav_file.setframerate(8000)  
    
    def save_transcript(transcript, call_sid):
        """Save transcript to CSV file"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        csv_file = 'transcripts.csv'
        
        if not os.path.exists(csv_file):
            with open(csv_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Timestamp', 'Call SID', 'Transcript', 'Audio File'])
        
        with open(csv_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, call_sid, transcript, audio_filename])
    
    def audio_stream():
        while True:
            try:
                message = ws.receive()
                if message is None:
                    break
                    
                packet = json.loads(message)
                event_type = packet.get('event')
                
                if event_type == 'start':
                    print('Streaming is starting')
                    continue
                elif event_type == 'stop':
                    print('\nStreaming has stopped')
                    check_and_clone_voice(audio_filename)
                    break
                elif event_type == 'media':
                    audio = base64.b64decode(packet['media']['payload'])
                    
                    pcm_audio = audioop.ulaw2lin(audio, 2)  
                    wav_file.writeframes(pcm_audio)
                    
                    request = speech.StreamingRecognizeRequest(audio_content=audio)
                    yield request
                    
            except Exception as e:
                print(f"Error processing message: {e}")
                break
    
    try:
        requests = audio_stream()
        responses = speech_client.streaming_recognize(streaming_config, requests)
        
        for response in responses:
            if not response.results:
                continue
                
            result = response.results[0]
            if not result.alternatives:
                continue
                
            transcript = result.alternatives[0].transcript
            
            if result.is_final:
                print(f"Final transcript: {transcript}")
                save_transcript(transcript, call_sid)
            else:
                print(f"Interim transcript: {transcript}")
                
    except Exception as e:
        print(f'Error in streaming recognition: {e}')
    finally:
        print('Streaming ended')
        wav_file.close()  
        
def check_and_clone_voice(audio_filename):
    import requests
    data = request.get_json()
    name = data['name'].strip()
    email = data['email'].strip()

    url = "https://api.sws.speechify.com/v1/voices"

    files = { "sample": ("audio.wav", open(audio_filename, "rb"), "audio/wav") }
    payload = {
        "name": {"Iowa State Ivy Showcase #1"},
        "consent": f"\"fullName\": \"{name}\", \"email\": \"{email}\" "
    }
    headers = {
        "accept": "*/*",
        "Authorization": f"{os.getenv('SPEECHIFY_API_KEY')}"
    }

    response = requests.post(url, data=payload, files=files, headers=headers)

    print(response.text)

if __name__ == '__main__':
    port = int(os.getenv('HTTP_SERVER_PORT', 8080))
    print(f'Server running at {NGROK_URL}')
    print(f'To make a call, send a POST request to {NGROK_URL}/make-call')
    
    app.run(port=port, debug=True)