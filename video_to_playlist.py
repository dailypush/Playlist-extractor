import os
import sys
import glob
import csv
import logging
from concurrent.futures import ThreadPoolExecutor
from moviepy.editor import *
from pydub import AudioSegment
from shazamio import Shazam

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def extract_audio(video_path, audio_path):
    video = VideoFileClip(video_path)
    audio = video.audio
    audio.write_audiofile(audio_path)

def split_audio(audio_path, segment_length=30):
    audio = AudioSegment.from_file(audio_path)
    audio_length = len(audio)
    segments = []

    for i in range(0, audio_length, segment_length * 1000):
        start = i
        end = i + segment_length * 1000
        segment = audio[start:end]
        segments.append(segment)

    return segments

async def identify_songs(segments):
    shazam = Shazam()
    identified_songs = []

    for segment in segments:
        segment.export("temp_audio_segment.wav", format="wav")
        matches = await shazam.recognize_song("temp_audio_segment.wav")
        if matches:
            top_match = matches[0]
            song_title = top_match['title']
            song_artist = top_match['subtitle']
            song_id = (song_title, song_artist)
            if song_id not in identified_songs:
                identified_songs.append(song_id)

    if os.path.exists("temp_audio_segment.wav"):
        os.remove("temp_audio_segment.wav")

    return identified_songs

def generate_csv(identified_songs, output_csv):
    with open(output_csv, mode='w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['Title', 'Artist']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for song in identified_songs:
            writer.writerow({'Title': song[0], 'Artist': song[1]})

async def process_video(video_path, output_csv):
    logger.info(f"Processing video: {video_path}")
    print(f"Processing video: {video_path}")

    audio_path = "temp_audio_file.wav"
    extract_audio(video_path, audio_path)
    segments = split_audio(audio_path)
    identified_songs = await identify_songs(segments)
    generate_csv(identified_songs, output_csv)

    if os.path.exists(audio_path):
        os.remove(audio_path)

    logger.info(f"Finished processing video: {video_path}")
    print(f"Finished processing video: {video_path}")

def find_mp4_files(folder_path):
    mp4_files = glob.glob(os.path.join(folder_path, "*.mp4"))
    return mp4_files

async def main(video_paths):
    with ThreadPoolExecutor() as executor:
        futures = []
        for video_path in video_paths:
            output_csv = os.path.splitext(video_path)[0] + ".csv"
            future = executor.submit(asyncio.run, process_video(video_path, output_csv))
            futures.append(future)

        for future in futures:
            future.result()

if __name__ == "__main__":
    import asyncio

    if len(sys.argv) < 2:
        print("Usage: python video_to_playlist.py [path/to/your/folder]")
        print("No folder path provided, using default folder: /app/data")
        folder_path = "/app/data"
    else:
        folder_path = sys.argv[1]

    video_paths = find_mp4_files(folder_path)
    asyncio.run(main(video_paths))

