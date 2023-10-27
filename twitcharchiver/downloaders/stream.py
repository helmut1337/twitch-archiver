"""
Module for downloading currently live Twitch broadcasts.
"""
import os
import shutil
import tempfile

from datetime import datetime, timezone
from glob import glob
from math import floor
from operator import attrgetter
from pathlib import Path
from time import sleep

import m3u8
import requests

from twitcharchiver.vod import Vod
from twitcharchiver.channel import Channel
from twitcharchiver.downloader import Downloader
from twitcharchiver.downloaders.video import MpegSegment
from twitcharchiver.exceptions import TwitchAPIErrorNotFound, UnsupportedStreamPartDuration, StreamDownloadError, \
    StreamSegmentDownloadError, StreamFetchError, StreamOfflineError
from twitcharchiver.utils import time_since_date, safe_move, build_output_dir_name


class StreamSegmentList:
    """
    Parses and stores segments of a Twitch livestream and the parts they are derived from.
    """
    def __init__(self, stream_created_at: float, align_segments: bool = True, start_id: int = 0):
        self.segments: dict[int: StreamSegment] = {}
        self.current_id = start_id + 1
        self._align_segments = align_segments
        self.stream_created_at = stream_created_at

    def add_part(self, part):
        # generate part id from timestamp if we are aligning segments
        if self._align_segments:
            _parent_segment_id = self._get_id_for_part(part)

        # otherwise use our current id
        else:
            _parent_segment_id = self.current_id

        # create parent segment if one doesn't yet exist
        if _parent_segment_id not in self.segments.keys():
            self.current_id = _parent_segment_id
            self.segments[_parent_segment_id] = StreamSegment(_parent_segment_id)

        # create new segment and increment ID if current segment complete
        if len(self.get_segment_by_id(_parent_segment_id).parts) == 5:
            _parent_segment_id += 1
            self.current_id = _parent_segment_id
            self.segments[_parent_segment_id] = StreamSegment(_parent_segment_id)

        # append part to parent segment
        self.segments[_parent_segment_id].add_part(part)

    def _get_id_for_part(self, part):
        # maths for determining the id of a given part based on its timestamp and the stream creation time
        return floor((4 + (part.timestamp - self.stream_created_at)) / 10)

    def is_segment_present(self, segment_id: int):
        return segment_id in self.segments.keys()

    def get_segment_by_id(self, segment_id: int):
        """
        Fetches the segment with the provided ID.

        :param segment_id: segment id to fetch
        :return: Segment which matches provided ID
        :rtype: StreamSegment
        """
        return self.segments[segment_id]

    def get_completed_segment_ids(self):
        """
        Gathers and returns the ids of all segments with 5 parts.

        :return:
        """
        _segment_ids: set[int] = set()
        for _segment in self.segments:
            if len(self.segments[_segment].parts) == 5:
                _segment_ids.add(self.segments[_segment].v_id)

        return _segment_ids

    def pop_segment(self, seg_id):
        """
        Pops the provided segment ID off of the list of segments.

        :param seg_id: id of segment to remove and return
        :return: segment which matches the id
        :rtype: StreamSegment
        """
        return self.segments.pop(seg_id)


class StreamSegment:
    def __init__(self, segment_id: int):
        """
        Defines a video segment made up of 5 StreamSegment parts.
        """
        self.parts: list[StreamSegment.Part] = []
        self.id: int = segment_id
        self.duration: float = 0

    class Part:
        def __init__(self, part):
            """
            Defines a part of a segment.
            """
            self.url: str = part.uri
            self.timestamp: float = part.program_date_time.replace(tzinfo=None).timestamp()
            self.duration: float = part.duration
            self.title = part.title

        def __repr__(self):
            return str({'url': self.url, 'timestamp': self.timestamp, 'duration': self.duration})

        def __eq__(self, other):
            if isinstance(other, StreamSegment.Part):
                return self.url == other.url
            else:
                return False

        def __hash__(self):
            return hash(self.url)

    def add_part(self, part: Part):
        """
        Adds a part to the segment and updates the duration.

        :param part: part to add to segment
        """
        self.parts.append(part)
        self.duration += part.duration

    def is_full(self):
        """
        Checks if the segment had five parts.

        :return: True if segment is full
        :rtype: bool
        """
        if len(self.parts) == 5:
            return True

        return False

    def __repr__(self):
        return str({'id': self.id, 'duration': self.duration, 'parts': self.parts})

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            if self.parts == other.parts:
                return True

        return False

    def __hash__(self):
        return hash(self.parts)


class Stream(Downloader):
    """
    Class which handles the downloading of video files for a given Twitch stream.
    """
    _quality: str = ''

    def __init__(self, channel: Channel, parent_dir: Path = Path(os.getcwd()), quality: str = 'best',
                 quiet: bool = 'False'):
        """
        Class constructor.

        :param channel: Channel to be downloaded
        :type channel: Channel
        :param parent_dir: Parent directory in which to create VOD directory and download to
        :type parent_dir: str
        :param quality: Quality of the stream to download in the format [resolution]p[framerate]
        :type quality: str
        :param quiet: True suppresses progress reporting
        :type quiet: bool
        """
        super().__init__(parent_dir, quiet)

        self.__setattr__('_quality', quality)

        # create segments for combining with archived VOD parts. If true we will try to recreate the segment numbering
        # scheme Twitch uses, otherwise we use our own numbering scheme. Only used when archiving a live stream without
        # a VOD.
        self._align_segments: bool = True

        # buffers and progress tracking
        self._index_uri: str = ""
        self._incoming_part_buffer: list[StreamSegment.Part] = []
        self._download_queue: StreamSegmentList = None
        self._completed_segments: set[MpegSegment] = set()
        self._processed_parts: set[StreamSegment.Part] = set()
        self._last_part_announce: float = datetime.now(timezone.utc).timestamp()

        # channel-specific vars
        self.output_dir: Path = None
        self.channel: Channel = channel
        self.stream: Vod = Vod()

        # perform setup
        self._do_setup()

    def __repr__(self):
        return str({'channel': self.channel, 'index_uri': self._index_uri, 'stream': self.stream})

    def start(self):
        """
        Begins downloading the stream for the channel until stopped or stream ends.
        """
        _start_timestamp: float = datetime.utcnow().timestamp()

        # loop until stream ends
        while True:
            self.single_download_pass()

            # assume stream has ended once >20s has passed since the last segment was advertised
            #   if parts remain in the buffer, we need to download them whether there are 5 parts or not
            if time_since_date(self._last_part_announce) > 20:
                self._get_final_segment()
                return

            # sleep if processing time < 4s before checking for new segments
            _loop_time = int(datetime.utcnow().timestamp() - _start_timestamp)
            if _loop_time < 4:
                sleep(4 - _loop_time)

    def single_download_pass(self):
        """
        Used to fetch and download stream segments without looping. This is used for creating a stream buffer at the
        start of archiving in case a VOD never becomes available, in which case the previously broadcast segments
        would be lost.
        """
        try:
            self._fetch_advertised_parts()
            self._build_download_queue()
            self._download_queued_segments()

        # stream offline
        except TwitchAPIErrorNotFound:
            self._log.info('%s is offline or stream ended.', self.channel.name)
            if self._download_queue:
                self._get_final_segment()

        # catch any other exception
        except BaseException as e:
            raise StreamDownloadError(self.channel.name, e) from e

    def _do_setup(self):
        """
        Performs required setup prior to starting download.
        """
        self._log.debug('Fetching required stream information.')
        self.stream = Vod.from_stream_json(self.channel.get_stream_info())

        if not self.stream:
            self._log.info('%s is offline.', self.channel.name)
            raise StreamOfflineError(self.channel)

        # ensure enough time has passed for VOD api to update before archiving. this is important as it changes
        # the way we archive if the stream is not being archived to a VOD.
        self._log.debug('Current stream length: %s', self.stream.duration)

        # while we wait for the api to update we must build a temporary buffer of any parts advertised in the
        # meantime in case there is no vod and thus no way to retrieve them after the fact
        if self.stream.duration < 120 and not self.stream.v_id:
            self._buffer_stream(self.stream.duration)

        # fetch VOD ID for output directory, disable segment alignment if there is no paired VOD ID
        self.stream.v_id = 0 or self.channel.get_broadcast_vod_id()
        if not self.stream.v_id:
            self._align_segments = False

        # if a paired VOD exists for the stream we can discard our previous buffer
        else:
            if self.output_dir:
                shutil.rmtree(Path(self.output_dir))

            # build and create output directory
            self.output_dir = build_output_dir_name(self.stream.title, self.stream.created_at, self.stream.v_id)
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        # get existing parts to resume counting if archiving halted
        self._completed_segments.update([MpegSegment(int(Path(p).name.removesuffix('.ts')), 10)
                                         for p in glob(str(Path(self.output_dir, '*.ts')))])

        _latest_segment = MpegSegment(-1, 0)
        if self._completed_segments:
            # set start segment for download queue
            _latest_segment = max(self._completed_segments, key=attrgetter('id'))

        # pass stream created to segment list to be used for determining part and segment ids
        self._download_queue = StreamSegmentList(self.stream.created_at, self._align_segments, _latest_segment.id)

        # fetch index
        self._index_uri = self.channel.get_stream_index(self._quality)

    def _buffer_stream(self, stream_length: int):
        """
        Builds a temporary buffer when a stream has just started to ensure the Twitch API has updated when we
        attempt to fetch VOD information.

        :param stream_length: time in seconds stream has been live
        :type stream_length: int
        :return:
        """
        self._log.debug('Stream began less than 120s ago, delaying archival start until VOD API updated.')

        # create temporary download directory
        self.output_dir = build_output_dir_name(self.stream.title, self.stream.created_at)
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        # download new parts every 4s
        for _ in range(int((120 - stream_length) / 4)):
            _start_timestamp: float = datetime.utcnow().timestamp()
            self.single_download_pass()

            # wait if less than 4s passed since grabbing more parts
            _loop_time = int(datetime.utcnow().timestamp() - _start_timestamp)
            if _loop_time < 4:
                sleep(4 - _loop_time)

        # wait any remaining time
        sleep((120 - stream_length) % 4)

    def _fetch_advertised_parts(self):
        _last_part_announce: float = datetime.now(timezone.utc).timestamp()
        # attempt to grab new parts from Twitch
        for _ in range(6):
            try:
                if _ > 4:
                    raise StreamFetchError(self.channel.name, 'Request time out while fetching new segments.')

                # fetch advertised stream parts
                self._log.debug('Fetching incoming stream parts.')
                for _part in [StreamSegment.Part(_p) for _p in
                              m3u8.loads(self.channel.get_stream_playlist(self._index_uri)).segments]:
                    # add new parts to part buffer and update last announcement timestamp
                    if _part not in self._processed_parts:
                        self._processed_parts.add(_part)
                        self._incoming_part_buffer.append(_part)
                        self._last_part_announce = datetime.now(timezone.utc).timestamp()
                break

            # retry if request times out
            except requests.exceptions.ConnectTimeout:
                self._log.debug('Timed out attempting to fetch new stream segments, retrying. (Attempt %s)', _ + 1)
                continue

    def _build_download_queue(self):
        self._log.debug('Building download queue.')
        _unsupported_parts = set()
        # add parts to the associated segment
        for _part in self._incoming_part_buffer:
            if _part.title != 'live':
                self._log.debug('Blocking advertisement part %s.', _part)
                continue

            # some streams have part lengths other than the default of 2.0. these cannot be aligned, and so we raise
            # an error if we encounter more than one if we are attempting to align the segments. we check for >1
            # instead of just 1 as the final part in the stream is often shorter than 2.0.
            if self._align_segments and _part.duration != 2.0:
                self._log.debug('Found part with unsupported duration (%s).', _part.duration)
                _unsupported_parts.add(_part)

            if len(_unsupported_parts) > 1:
                raise UnsupportedStreamPartDuration

            # add part to segment download queue
            self._log.debug('Adding part %s to download queue.', _part)
            self._download_queue.add_part(_part)

        # wipe part buffer
        self._incoming_part_buffer = []

    def _download_queued_segments(self):
        self._log.debug('Processing download queue.')
        for _segment_id in self._download_queue.get_completed_segment_ids():
            self._download_segment(self._download_queue.pop_segment(_segment_id))

    def _download_segment(self, segment: StreamSegment):
        # generate buffer file path
        _temp_buffer_file = Path(tempfile.gettempdir(), 'twitch-archiver',
                                 str(self.stream.s_id), str(f'{segment.id:05d}' + '.ts'))
        _temp_buffer_file.parent.mkdir(parents=True, exist_ok=True)

        # begin retry loop for download
        for _ in range(6):
            if _ > 4:
                self._log.error('Maximum attempts reached while downloading segment %s.', segment.id)
                return

            self._log.debug('Downloading segment %s to %s.', segment.id, _temp_buffer_file)
            with open(_temp_buffer_file, 'wb') as _tmp_file:
                # iterate through each part of the segment, downloading them in order
                for _part in segment.parts:
                    try:
                        _r = requests.get(_part.url, stream=True, timeout=5)

                        if _r.status_code != 200:
                            return

                        # write part to file
                        for chunk in _r.iter_content(chunk_size=262144):
                            _tmp_file.write(chunk)

                    except requests.exceptions.RequestException as e:
                        self._log.debug('Error downloading stream segment %s: %s', segment.id, str(e))

            # move finished ts file to destination storage
            try:
                safe_move(Path(_temp_buffer_file), Path(self.output_dir, str(f'{segment.id:05d}' + '.ts')))
                self._log.debug('Stream segment: %s completed.', segment.id)
                break

            except BaseException as e:
                raise StreamSegmentDownloadError(segment.id, self.channel.name, e) from e

    def _get_final_segment(self):
        """
        Downloads and stores the final stream segment.
        """
        # ensure final segment present
        if self._download_queue.is_segment_present(self._download_queue.current_id):
            self._log.debug('Fetching final stream segment.', self.channel.name)
            self._download_segment(self._download_queue.get_segment_by_id(self._download_queue.current_id))

    def delete_segments(self):
        """
        Deletes all downloaded segments.
        """