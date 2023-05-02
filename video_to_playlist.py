import os
import sys
import csv
import glob
import logging
import base64
import hashlib
import hmac
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from moviepy.editor import VideoFileClip
from pydub import AudioSegment
from pydub.silence import split_on_silence
import acrcloud
from tqdm import tqdm
import json
from configparser import ConfigParser
from pydub.utils import mediainfo

# read the configuration file
config = ConfigParser()
config.read('config.ini')

acrcloud_config = {
    'host': 'identify-eu-west-1.acrcloud.com',
    'access_key': config.get('secrets', 'ACCESS_KEY'),
    'access_secret': config.get('secrets', 'ACCESS_SECRET'),
    'timeout': 10
}

acrcloud_recognizer = acrcloud.ACRCloudRecognizer(acrcloud_config)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def extract_audio(video_path, audio_path):
    logger.info(f"Extracting audio from video: {video_path}")
    clip = VideoFileClip(video_path)
    clip.audio.write_audiofile(audio_path)


def split_audio(audio_path, chunk_length=20000):
    logger.info("Splitting audio into segments")
    audio = AudioSegment.from_wav(audio_path)

    segments = []
    audio_length = len(audio)
    for i in tqdm(range(0, audio_length, chunk_length), desc="Splitting audio", ncols=100, unit="seg"):
        start_time = i
        end_time = min(i + chunk_length, audio_length)
        segment = audio[start_time:end_time]
        segments.append(segment)

    return segments

def create_acrcloud_request_data(segment):
    segment.export("temp_audio_segment.wav", format="wav")
    f = open("temp_audio_segment.wav", "rb")
    sample_bytes = os.path.getsize("temp_audio_segment.wav")
    files = [("sample", ("temp_audio_segment.wav", f, "audio/wav"))]

    timestamp = time.time()
    string_to_sign = (
        "POST"
        + "\n"
        + "/v1/identify"
        + "\n"
        + acrcloud_config["access_key"]
        + "\n"
        + "audio"
        + "\n"
        + "1"
        + "\n"
        + str(timestamp)
    )

    sign = base64.b64encode(
        hmac.new(
            acrcloud_config["access_secret"].encode("ascii"),
            string_to_sign.encode("ascii"),
            digestmod=hashlib.sha1,
        ).digest()
    ).decode("ascii")

    data = {
        "access_key": acrcloud_config["access_key"],
        "sample_bytes": sample_bytes,
        "timestamp": str(timestamp),
        "signature": sign,
        "data_type": "audio",
        "signature_version": "1",
    }

    return files, data

def send_acrcloud_request(files, data):
    requrl = "https://" + acrcloud_config["host"]
    
    # Print the request data before sending
    print(f"Sending request to ACRCloud with data: {data}")
    
    r = requests.post(requrl, files=files, data=data)
    r.encoding = "utf-8"
    result = r.text
    
    # Close the file after the request is completed
    files[0][1][1].close()

    return json.loads(result)

def process_acrcloud_result(result):
    if result['status']['msg'] == 'Success':
        song_title = result['metadata']['music'][0]['title']
        song_artist = result['metadata']['music'][0]['artists'][0]['name']
        return (song_title, song_artist)
    else:
        return None

def identify_songs(segments):
    identified_songs = []

    for idx, segment in enumerate(segments):
        logger.info(f"Identifying song for segment {idx + 1}/{len(segments)}")
        
        files, data = create_acrcloud_request_data(segment)
        result = send_acrcloud_request(files, data)
        song_id = process_acrcloud_result(result)

        if song_id is not None and song_id not in identified_songs:
            identified_songs.append(song_id)
            logger.info(f"Identified song: '{song_id[0]}' by {song_id[1]}")
        else:
            logger.error(f"Error on segment {idx + 1}/{len(segments)}: {result}")

    if os.path.exists("temp_audio_segment.wav"):
        os.remove("temp_audio_segment.wav")

    return identified_songs
def generate_csv(identified_songs, output_csv):
    logger.info(f"Generating CSV playlist: {output_csv}")
    with open(output_csv, mode='w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['Title', 'Artist']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for song in identified_songs:
            writer.writerow({'Title': song[0], 'Artist': song[1]})


def process_video(video_path, output_csv):
    logger.info(f"Processing video: {video_path}")

    audio_filename = f"temp_audio_file_{os.path.basename(video_path)}.wav"
    audio_path = os.path.join(os.getcwd(), audio_filename)

    # Ensure the directory is writable
    audio_dir = os.path.dirname(audio_path)
    if not os.access(audio_dir, os.W_OK):
        logger.error(
            f"The directory '{audio_dir}' is not writable. Please check the permissions and try again.")
        return

    extract_audio(video_path, audio_path)
    segments = split_audio(audio_path)
    identified_songs = identify_songs(segments)

    generate_csv(identified_songs, output_csv)

    if os.path.exists(audio_path):
        os.remove(audio_path)

    logger.info(f"Finished processing video: {video_path}")


def find_mp4_files(folder_path):
    mp4_files = glob.glob(os.path.join(folder_path, "*.mp4"))
    return mp4_files


def main(video_paths):
    with ThreadPoolExecutor() as executor:
        tasks = []
        for video_path in video_paths:
            output_csv = os.path.splitext(video_path)[0] + ".csv"
            task = executor.submit(process_video, video_path, output_csv)
            tasks.append(task)

        for task in tasks:
            task.result()


if __name__ == "__main__":
    folder_path = sys.argv[1]
    video_paths = find_mp4_files(folder_path)
    main(video_paths)
