import sys
import os
from acrcloud.recognizer import ACRCloudRecognizer
from configparser import ConfigParser


# read the configuration file
config = ConfigParser()
config.read('config.ini')


config = {
    'host': 'identify-eu-west-1.acrcloud.com',
    'access_key': config.get('secrets', 'ACCESS_KEY'),
    'access_secret': config.get('secrets', 'ACCESS_SECRET'),
    'timeout': 10,
    'debug': True
}


'''This module can recognize ACRCloud by most of audio/video file.
        Audio: mp3, wav, m4a, flac, aac, amr, ape, ogg ...
        Video: mp4, mkv, wmv, flv, ts, avi ...
'''

acrcloud_sdk = os.path.dirname(os.path.realpath(__file__)) + "/acrcloud_sdk_python"
sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/acrcloud_sdk_python")
try:
    import acrcloud
except ImportError:
    print("import acrcloud_extr_tool error, please add acrcloud_extr_tool.py and its libs to system path")
    sys.exit(1)

class ACRCloudRecognizer(ACRCloudRecognizer):

    def recognize_by_file(self, file_path, start_seconds, rec_length=10):
        '''
        recognize by file path, and you can also set the rec_length, if None, it will be the default 10 seconds.
        :param file_path: query file path
        :param start_seconds: skip (start_seconds) seconds from from the beginning of (file_path)
        :param rec_length: audio length to recognize, if None, it will be the default 10 seconds.
        :return: result, json
        '''
        res = ''
        try:
            if not os.path.exists(file_path):
                return '{"status": {"msg": "file not exist", "code": 1001}}'
            fp = open(file_path, "rb")
            file_buffer = fp.read()
            fp.close()
            res = self.recognize_by_filebuffer(file_buffer, start_seconds, rec_length)
        except Exception as e:
            res = str({"status": {"msg": str(e), "code": 2000}})
        return res
