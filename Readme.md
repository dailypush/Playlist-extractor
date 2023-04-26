# Video to Playlist

This script processes MP4 video files and generates CSV playlists of songs identified throughout the videos. It uses the Shazam API to recognize songs in the video files and outputs a CSV file containing song titles and artist names for each video.

## Requirements

- Python 3.9 or newer
- FFmpeg (for the pydub library)

## Installation

1. Install the required Python libraries using the following command:

```bash
pip install -r requirements.txt
```

2. Install FFmpeg:

- **macOS** (using Homebrew):
  ```bash
  brew install ffmpeg
  ```
- **Windows**: Download the FFmpeg build for Windows from https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z, extract the files to a folder, and add the `bin` folder to your system's `PATH` environment variable.
- **Linux** (Ubuntu/Debian):
  ```bash
  sudo apt update
  sudo apt install ffmpeg
  ```
- **Linux** (Fedora):
  ```bash
  sudo dnf install ffmpeg
  ```

## Usage

Run the script with the path to the folder containing your MP4 video files as a command-line argument:

```bash
python video_to_playlist.py path/to/your/folder
```

Replace `path/to/your/folder` with the path to the folder containing your MP4 video files. The script will process all MP4 files in the specified folder and generate a CSV playlist for each input video file with the same name but with a `.csv` extension.

## Running with Docker

1. Build the Docker image:

```bash
docker build -t video-to-playlist .
```

2. Run the script in a Docker container:

```bash
docker run -v <path/to/your/folder>:/app/data video-to-playlist
```

Replace `<path/to/your/folder>` with the path to your folder containing the MP4 video files. The script will process the MP4 files and generate CSV playlists in the same folder.
```
