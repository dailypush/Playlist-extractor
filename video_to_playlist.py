import os
import argparse
import asyncio
import tempfile
from moviepy.editor import *
from pydub import AudioSegment
from shazamio import Shazam
from tqdm import tqdm

async def recognize_song(segment_file):
    shazam = Shazam()
    song = await shazam.recognize_song(data=open(segment_file, 'rb').read())
    return song

async def main():
    # Step 1: Parse command line arguments
    parser = argparse.ArgumentParser(description='Identify songs in MP4 files and create a playlist')
    parser.add_argument('directory', type=str, help='path to the directory containing the MP4 files')
    args = parser.parse_args()

    # Step 2: Identify the MP4 files
    mp4_files = [os.path.join(args.directory, f) for f in os.listdir(args.directory) if f.endswith('.mp4')]

    # Step 3: Extract audio from the MP4 files and split into segments
    song_names = []
    for mp4_file in mp4_files:
        clip = VideoFileClip(mp4_file)
        audio_file = mp4_file.replace('.mp4', '.mp3')
        clip.audio.write_audiofile(audio_file)
        sound = AudioSegment.from_mp3(audio_file)
        duration = len(sound) / 1000
        segment_duration = 5 * 60 * 1000  # 5 minutes in milliseconds
        start_time = 0
        end_time = segment_duration
        with tqdm(total=duration, desc=f'Processing {mp4_file}') as pbar:
            while end_time <= duration:
                with tempfile.NamedTemporaryFile(suffix='.mp3') as tmp_file:
                    segment = sound[start_time:end_time]
                    segment.export(tmp_file.name, format='mp3')
                    song = await recognize_song(tmp_file.name)
                    if song:
                        song_names.append(song['track']['title'])
                    start_time += segment_duration
                    end_time += segment_duration
                    pbar.update(segment_duration / 1000)

    # Step 4: Create the playlist
    if song_names:
        with open('playlist.csv', 'w') as f:
            for song_name in song_names:
                f.write(f'{song_name}\n')
        print(f'Successfully created playlist with {len(song_names)} songs')
    else:
        print('No songs found')

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
