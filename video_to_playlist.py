import os
import sys
import csv
import glob
import logging
from concurrent.futures import ThreadPoolExecutor
from moviepy.editor import VideoFileClip
from pydub import AudioSegment
from pydub.silence import split_on_silence
from shazamio import Shazam
from tqdm import tqdm


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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


async def identify_songs(segments):
    shazam = Shazam()
    identified_songs = []

    for idx, segment in enumerate(segments):
        logger.info(f"Identifying song for segment {idx + 1}/{len(segments)}")
        segment.export("temp_audio_segment.wav", format="wav")
        result = await shazam.recognize_song("temp_audio_segment.wav")
        matches = result.get('hits', [])
        
        if matches:
            top_match = matches[0]
            song_title = top_match['track']['title']
            song_artist = top_match['track']['subtitle']
            song_id = (song_title, song_artist)
            if song_id not in identified_songs:
                identified_songs.append(song_id)

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

    audio_path = "temp_audio_file.wav"
    
    # Ensure the directory is writable
    audio_dir = os.path.dirname(audio_path)
    if not os.access(audio_dir, os.W_OK):
        logger.error(f"The directory '{audio_dir}' is not writable. Please check the permissions and try again.")
        return

    extract_audio(video_path, audio_path)
    segments = split_audio(audio_path)
    identified_songs = await identify_songs(segments)
    generate_csv(identified_songs, output_csv)

    if os.path.exists(audio_path):
        os.remove(audio_path)

    logger.info(f"Finished processing video: {video_path}")


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
        logger.info("Usage: python video_to_playlist.py [path/to/your/folder]")
        logger.info("No folder path provided, using default folder: /app/data")
        folder_path = "/app/data"
    else:
        folder_path = sys.argv[1]

    video_paths = find_mp4_files(folder_path)
    if not video_paths:
        logger.error(f"No MP4 files found in folder: {folder_path}")
    else:
        logger.info(f"Found {len(video_paths)} MP4 files in folder: {folder_path}")
        asyncio.run(main(video_paths))
