#
#      Copyright (C) 2012 Tommy Winther
#      http://tommy.winther.nu
#
#  This Program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2, or (at your option)
#  any later version.
#
#  This Program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this Program; see the file LICENSE.txt.  If not, write to
#  the Free Software Foundation, 675 Mass Ave, Cambridge, MA 02139, USA.
#  http://www.gnu.org/copyleft/gpl.html
#
import StringIO
import os, sys
import simplejson
import datetime
import threading
import time
import urllib2
from xml.etree import ElementTree
from strings import *
from HTMLParser import HTMLParser
import ysapi
import buggalo
import xbmc
import xbmcgui
import xbmcvfs
from sqlite3 import dbapi2 as sqlite3
import tarfile
import zipfile

SETTINGS_TO_CHECK = ['source', 'youseetv.category', 'xmltv.file', 'xmltv.logo.folder', 'ontv.url', 'json.url','xmltv.url']

class Channel(object):
    def __init__(self, id, title, logo = None, streamUrl = None, visible = True, weight = -1):
        self.id = id
        self.title = title
        self.logo = logo
        self.streamUrl = streamUrl
        self.visible = visible
        self.weight = weight

    def isPlayable(self):
        return hasattr(self, 'streamUrl') and self.streamUrl

    def __repr__(self):
        return 'Channel(id=%s, title=%s, logo=%s, streamUrl=%s)' \
               % (self.id, self.title, self.logo, self.streamUrl)

class Program(object):
    def __init__(self, channel, title, startDate, endDate, description, imageLarge = None, imageSmall=None, notificationScheduled = None):
        """

        @param channel:
        @type channel: source.Channel
        @param title:
        @param startDate:
        @param endDate:
        @param description:
        @param imageLarge:
        @param imageSmall:
        """
        self.channel = channel
        self.title = title
        self.startDate = startDate
        self.endDate = endDate
        self.description = description
        self.imageLarge = imageLarge
        self.imageSmall = imageSmall
        self.notificationScheduled = notificationScheduled

    def __repr__(self):
        return 'Program(channel=%s, title=%s, startDate=%s, endDate=%s, description=%s, imageLarge=%s, imageSmall=%s)' % \
            (self.channel, self.title, self.startDate, self.endDate, self.description, self.imageLarge, self.imageSmall)

class SourceException(Exception):
    pass

class SourceUpdateInProgressException(SourceException):
    pass

class SourceUpdateCanceledException(SourceException):
    pass

class SourceNotConfiguredException(SourceException):
    pass

class DatabaseSchemaException(sqlite3.DatabaseError):
    pass
    
class MyHTMLParser(HTMLParser):

    def __init__(self, fh):
        """
        {fh} must be an input stream returned by open() or urllib2.urlopen()
        """
        HTMLParser.__init__(self)
        self.link = ""
        self.feed(fh.read())
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            if attrs[0][0] == 'href':
                self.link = attrs[0][1]
    def get_fileids(self):
        return self.link

class Source(object):
    KEY = "undefined"
    SOURCE_DB = 'source.db'

    def __init__(self, addon, cachePath):
        self.cachePath = cachePath
        self.updateInProgress = False
        buggalo.addExtraData('source', self.KEY)
        for key in SETTINGS_TO_CHECK:
            buggalo.addExtraData('setting: %s' % key, ADDON.getSetting(key))

        self.channelList = list()
        self.player = xbmc.Player()
        self.osdEnabled = addon.getSetting('enable.osd') == 'true'

        databasePath = os.path.join(self.cachePath, self.SOURCE_DB)
        for retries in range(0, 3):
            try:
                self.conn = sqlite3.connect(databasePath, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread = False)
                self.conn.execute('PRAGMA foreign_keys = ON')
                self.conn.row_factory = sqlite3.Row

                # create and drop dummy table to check if database is locked
                c = self.conn.cursor()
                c.execute('CREATE TABLE database_lock_check(id TEXT PRIMARY KEY)')
                c.execute('DROP TABLE database_lock_check')
                c.close()

                self._createTables()
                self.settingsChanged = self.wasSettingsChanged(addon)
                break

            except sqlite3.OperationalError, ex:
                raise SourceUpdateInProgressException(ex)
            except sqlite3.DatabaseError:
                self.conn = None
                try:
                    os.unlink(databasePath)
                except OSError:
                    pass
                xbmcgui.Dialog().ok(ADDON.getAddonInfo('name'), strings(DATABASE_SCHEMA_ERROR_1),
                    strings(DATABASE_SCHEMA_ERROR_2), strings(DATABASE_SCHEMA_ERROR_3))

        if self.conn is None:
            raise SourceNotConfiguredException()

    def close(self):
        #self.conn.rollback() # rollback any non-commit'ed changes to avoid database lock
        if self.player.isPlaying():
            self.player.stop()
        self.conn.close()

    def wasSettingsChanged(self, addon):
        settingsChanged = False
        noRows = True
        count = 0

        c = self.conn.cursor()
        c.execute('SELECT * FROM settings')
        for row in c:
            noRows = False
            key = row['key']
            if SETTINGS_TO_CHECK.count(key):
                count += 1
                if row['value'] != addon.getSetting(key):
                    settingsChanged = True

        if count != len(SETTINGS_TO_CHECK):
            settingsChanged = True

        if settingsChanged or noRows:
            for key in SETTINGS_TO_CHECK:
                value = addon.getSetting(key).decode('utf-8', 'ignore')
                c.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', [key, value])
                if not c.rowcount:
                    c.execute('UPDATE settings SET value=? WHERE key=?', [value, key])
            self.conn.commit()

        c.close()
        print 'Settings changed: ' + str(settingsChanged)
        #return True # Uncomment to force cache regeneration every run, for debug prp only
        return settingsChanged

    def getDataFromExternal(self, date, progress_callback = None):
        """
        Retrieve data from external as a list or iterable. Data may contain both Channel and Program objects.
        The source may choose to ignore the date parameter and return all data available.

        @param date: the date to retrieve the data for
        @param progress_callback:
        @return:
        """
        raise SourceException('getDataFromExternal not implemented!')

    def isCacheExpired(self, date = datetime.datetime.now()):
        return self.settingsChanged or self._isChannelListCacheExpired() or self._isProgramListCacheExpired(date)

    def updateChannelAndProgramListCaches(self, date = datetime.datetime.now(), progress_callback = None, clearExistingProgramList = True):
        self.updateInProgress = True
        dateStr = date.strftime('%Y-%m-%d')
        c = self.conn.cursor()
        try:
            xbmc.log('[script.tvguide] Updating caches...', xbmc.LOGDEBUG)
            if progress_callback:
                progress_callback(0)

            if self.settingsChanged:
                c.execute('DELETE FROM channels WHERE source=?', [self.KEY])
                c.execute('DELETE FROM programs WHERE source=?', [self.KEY])
                c.execute("DELETE FROM updates WHERE source=?", [self.KEY])
            self.settingsChanged = False # only want to update once due to changed settings

            if clearExistingProgramList:
                c.execute("DELETE FROM updates WHERE source=?", [self.KEY]) # cascades and deletes associated programs records
            else:
                c.execute("DELETE FROM updates WHERE source=? AND date=?", [self.KEY, dateStr]) # cascades and deletes associated programs records

            # programs updated
            c.execute("INSERT INTO updates(source, date, programs_updated) VALUES(?, ?, ?)", [self.KEY, dateStr, datetime.datetime.now()])
            updatesId = c.lastrowid

            imported = imported_channels = imported_programs = 0
            for item in self.getDataFromExternal(date, progress_callback):
                imported += 1

                if imported % 10000 == 0:
                    self.conn.commit()

                if isinstance(item, Channel):
                    imported_channels += 1
                    channel = item
                    c.execute('INSERT OR IGNORE INTO channels(id, title, logo, stream_url, visible, weight, source) VALUES(?, ?, ?, ?, ?, (CASE ? WHEN -1 THEN (SELECT COALESCE(MAX(weight)+1, 0) FROM channels WHERE source=?) ELSE ? END), ?)', [channel.id, channel.title, channel.logo, channel.streamUrl, channel.visible, channel.weight, self.KEY, channel.weight, self.KEY])
                    if not c.rowcount:
                        c.execute('UPDATE channels SET title=?, logo=?, stream_url=?, visible=(CASE ? WHEN -1 THEN visible ELSE ? END), weight=(CASE ? WHEN -1 THEN weight ELSE ? END) WHERE id=? AND source=?',
                            [channel.title, channel.logo, channel.streamUrl, channel.weight, channel.visible, channel.weight, channel.weight, channel.id, self.KEY])

                elif isinstance(item, Program):
                    imported_programs += 1
                    program = item
                    if isinstance(program.channel, Channel):
                        channel = program.channel.id
                    else:
                        channel = program.channel

                    c.execute('INSERT INTO programs(channel, title, start_date, end_date, description, image_large, image_small, source, updates_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [channel, program.title, program.startDate, program.endDate, program.description, program.imageLarge, program.imageSmall, self.KEY, updatesId])

            # channels updated
            c.execute("UPDATE sources SET channels_updated=? WHERE id=?", [datetime.datetime.now(),self.KEY])
            self.conn.commit()

            if imported_channels == 0 or imported_programs == 0:
                raise SourceException('No channels or programs imported')

        except SourceUpdateCanceledException:
            # force source update on next load
            c.execute('UPDATE sources SET channels_updated=? WHERE id=?', [datetime.datetime.fromtimestamp(0), self.KEY])
            c.execute("DELETE FROM updates WHERE source=?", [self.KEY]) # cascades and deletes associated programs records
            self.conn.commit()

        except Exception, ex:
            import traceback as tb
            import sys
            (type, value, traceback) = sys.exc_info()
            tb.print_exception(type, value, traceback)

            try:
                self.conn.rollback()
            except sqlite3.OperationalError:
                pass # no transaction is active

            try:
                # invalidate cached data
                c.execute('UPDATE sources SET channels_updated=? WHERE id=?', [datetime.datetime.fromtimestamp(0), self.KEY])
                self.conn.commit()
            except sqlite3.OperationalError:
                pass # database is locked

            raise SourceException(ex)
        finally:
            self.updateInProgress = False
            c.close()

    def getChannel(self, id):
        c = self.conn.cursor()
        c.execute('SELECT * FROM channels WHERE source=? AND id=?', [self.KEY, id])
        row = c.fetchone()
        channel = Channel(row['id'], row['title'],row['logo'], row['stream_url'], row['visible'], row['weight'])
        c.close()

        return channel

    def getNextChannel(self, currentChannel):
        channels = self.getChannelList()
        idx = channels.index(currentChannel)
        idx += 1
        if idx > len(channels) - 1:
            idx = 0
        return channels[idx]

    def getPreviousChannel(self, currentChannel):
        channels = self.getChannelList()
        idx = channels.index(currentChannel)
        idx -= 1
        if idx < 0:
            idx = len(channels) - 1
        return channels[idx]

    def getChannelList(self):
        # cache channelList in memory
        if not self.channelList:
            self.channelList = self._retrieveChannelListFromDatabase()

        return self.channelList

    def _storeChannelListInDatabase(self, channelList):
        c = self.conn.cursor()
        for idx, channel in enumerate(channelList):
            c.execute('INSERT OR IGNORE INTO channels(id, title, logo, stream_url, visible, weight, source) VALUES(?, ?, ?, ?, ?, (CASE ? WHEN -1 THEN (SELECT COALESCE(MAX(weight)+1, 0) FROM channels WHERE source=?) ELSE ? END), ?)', [channel.id, channel.title, channel.logo, channel.streamUrl, channel.visible, channel.weight, self.KEY, channel.weight, self.KEY])
            if not c.rowcount:
                c.execute('UPDATE channels SET title=?, logo=?, stream_url=?, visible=?, weight=(CASE ? WHEN -1 THEN weight ELSE ? END) WHERE id=? AND source=?', [channel.title, channel.logo, channel.streamUrl, channel.visible, channel.weight, channel.weight, channel.id, self.KEY])

        c.execute("UPDATE sources SET channels_updated=? WHERE id=?", [datetime.datetime.now(), self.KEY])
        self.channelList = None
        self.conn.commit()

    def _retrieveChannelListFromDatabase(self, onlyVisible = True):
        c = self.conn.cursor()
        channelList = list()
        if onlyVisible:
            c.execute('SELECT * FROM channels WHERE source=? AND visible=? ORDER BY weight', [self.KEY, True])
        else:
            c.execute('SELECT * FROM channels WHERE source=? ORDER BY weight', [self.KEY])
        for row in c:
            channel = Channel(row['id'], row['title'],row['logo'], row['stream_url'], row['visible'], row['weight'])
            channelList.append(channel)
        c.close()
        return channelList

    def _isChannelListCacheExpired(self):
        try:
            c = self.conn.cursor()
            c.execute('SELECT channels_updated FROM sources WHERE id=?', [self.KEY])
            row = c.fetchone()
            if not row:
                return True
            lastUpdated = row['channels_updated']
            c.close()

            today = datetime.datetime.now()
            return lastUpdated.day != today.day
        except TypeError:
            return True

    def getCurrentProgram(self, channel):
        """

        @param channel:
        @type channel: source.Channel
        @return:
        """
        program = None
        now = datetime.datetime.now()
        c = self.conn.cursor()
        c.execute('SELECT * FROM programs WHERE channel=? AND source=? AND start_date <= ? AND end_date >= ?', [channel.id, self.KEY, now, now])
        row = c.fetchone()
        if row:
            program = Program(channel, row['title'], row['start_date'], row['end_date'], row['description'], row['image_large'], row['image_small'])
        c.close()

        return program

    def getNextProgram(self, program):
        nextProgram = None
        c = self.conn.cursor()
        c.execute('SELECT * FROM programs WHERE channel=? AND source=? AND start_date >= ? ORDER BY start_date ASC LIMIT 1', [program.channel.id, self.KEY, program.endDate])
        row = c.fetchone()
        if row:
            nextProgram = Program(program.channel, row['title'], row['start_date'], row['end_date'], row['description'], row['image_large'], row['image_small'])
        c.close()

        return nextProgram

    def getPreviousProgram(self, program):
        previousProgram = None
        c = self.conn.cursor()
        c.execute('SELECT * FROM programs WHERE channel=? AND source=? AND end_date <= ? ORDER BY start_date DESC LIMIT 1', [program.channel.id, self.KEY, program.startDate])
        row = c.fetchone()
        if row:
            previousProgram = Program(program.channel, row['title'], row['start_date'], row['end_date'], row['description'], row['image_large'], row['image_small'])
        c.close()

        return previousProgram

    def getProgramList(self, channels, startTime):
        """

        @param channels:
        @type channels: list of source.Channel
        @param startTime:
        @type startTime: datetime.datetime
        @return:
        """
        endTime = startTime + datetime.timedelta(hours = 2)
        programList = list()

        channelMap = dict()
        for c in channels:
            channelMap[c.id] = c

        c = self.conn.cursor()
        c.execute('SELECT p.*, (SELECT 1 FROM notifications n WHERE n.channel=p.channel AND n.program_title=p.title AND n.source=p.source) AS notification_scheduled FROM programs p WHERE p.channel IN (\'' + ('\',\''.join(channelMap.keys())) + '\') AND p.source=? AND p.end_date >= ? AND p.start_date <= ?', [self.KEY, startTime, endTime])
        for row in c:
            program = Program(channelMap[row['channel']], row['title'], row['start_date'], row['end_date'], row['description'], row['image_large'], row['image_small'], row['notification_scheduled'])
            programList.append(program)

        return programList

    def _isProgramListCacheExpired(self, date = datetime.datetime.now()):
        # check if data is up-to-date in database
        dateStr = date.strftime('%Y-%m-%d')
        c = self.conn.cursor()
        c.execute('SELECT programs_updated FROM updates WHERE source=? AND date=?', [self.KEY, dateStr])
        row = c.fetchone()
        today = datetime.datetime.now()
        expired = row is None or row['programs_updated'].day != today.day
        c.close()
        return expired


    def _downloadUrl(self, url):
        u = urllib2.urlopen(url, timeout=30)
        content = u.read()
        u.close()
            
        return content

    def setCustomStreamUrl(self, channel, stream_url):
        c = self.conn.cursor()
        c.execute("DELETE FROM custom_stream_url WHERE channel=?", [channel.id])
        c.execute("INSERT INTO custom_stream_url(channel, stream_url) VALUES(?, ?)", [channel.id, stream_url.decode('utf-8', 'ignore')])
        self.conn.commit()
        c.close()

    def getCustomStreamUrl(self, channel):
        c = self.conn.cursor()
        c.execute("SELECT stream_url FROM custom_stream_url WHERE channel=?", [channel.id])
        stream_url = c.fetchone()
        c.close()

        if stream_url:
            return stream_url[0]
        else:
            return None

    def deleteCustomStreamUrl(self, channel):
        c = self.conn.cursor()
        c.execute("DELETE FROM custom_stream_url WHERE channel=?", [channel.id])
        self.conn.commit()
        c.close()

    def isPlayable(self, channel):
        customStreamUrl = self.getCustomStreamUrl(channel)
        return customStreamUrl is not None or channel.isPlayable()

    def isPlaying(self):
        return self.player.isPlaying()

    def stop(self):
        self.player.stop()

    def play(self, channel, playBackStoppedHandler):
        threading.Timer(0.5, self.playInThread, [channel, playBackStoppedHandler]).start()

    @buggalo.buggalo_try_except({'method' : 'source.playThread'})
    def playInThread(self, channel, playBackStoppedHandler):
        customStreamUrl = self.getCustomStreamUrl(channel)
        if customStreamUrl:
            customStreamUrl = customStreamUrl.encode('utf-8', 'ignore')
            xbmc.log("Playing custom stream url: %s" % customStreamUrl)
            self.player.play(item = customStreamUrl, windowed = self.osdEnabled)

        elif channel.isPlayable():
            streamUrl = channel.streamUrl.encode('utf-8', 'ignore')
            xbmc.log("Playing : %s" % streamUrl)
            self.player.play(item = streamUrl, windowed = self.osdEnabled)

        while True:
            xbmc.sleep(250)
            if not self.player.isPlaying():
                break

        playBackStoppedHandler.onPlayBackStopped()

    def _createTables(self):
        c = self.conn.cursor()

        try:
            c.execute('SELECT major, minor, patch FROM version')
            (major, minor, patch) = c.fetchone()
            version = [major, minor, patch]
        except sqlite3.OperationalError:
            version = [0, 0, 0]

        try:
            if version < [1, 3, 0]:
                c.execute('CREATE TABLE IF NOT EXISTS custom_stream_url(channel TEXT, stream_url TEXT)')
                c.execute('CREATE TABLE version (major INTEGER, minor INTEGER, patch INTEGER)')
                c.execute('INSERT INTO version(major, minor, patch) VALUES(1, 3, 0)')

                # For caching data
                c.execute('CREATE TABLE sources(id TEXT PRIMARY KEY, channels_updated TIMESTAMP)')
                c.execute('CREATE TABLE updates(id INTEGER PRIMARY KEY, source TEXT, date TEXT, programs_updated TIMESTAMP)')
                c.execute('CREATE TABLE channels(id TEXT, title TEXT, logo TEXT, stream_url TEXT, source TEXT, visible BOOLEAN, weight INTEGER, PRIMARY KEY (id, source), FOREIGN KEY(source) REFERENCES sources(id) ON DELETE CASCADE)')
                c.execute('CREATE TABLE programs(channel TEXT, title TEXT, start_date TIMESTAMP, end_date TIMESTAMP, description TEXT, image_large TEXT, image_small TEXT, source TEXT, updates_id INTEGER, FOREIGN KEY(channel, source) REFERENCES channels(id, source) ON DELETE CASCADE, FOREIGN KEY(updates_id) REFERENCES updates(id) ON DELETE CASCADE)')
                c.execute('CREATE INDEX program_list_idx ON programs(source, channel, start_date, end_date)')
                c.execute('CREATE INDEX start_date_idx ON programs(start_date)')
                c.execute('CREATE INDEX end_date_idx ON programs(end_date)')

                # For active setting
                c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')

                # For notifications
                c.execute("CREATE TABLE notifications(channel TEXT, program_title TEXT, source TEXT, FOREIGN KEY(channel, source) REFERENCES channels(id, source) ON DELETE CASCADE)")

            if version < [1,3, 1]:
                # Recreate tables with FOREIGN KEYS as DEFERRABLE INITIALLY DEFERRED
                c.execute('UPDATE version SET major=1, minor=3, patch=1')
                c.execute('DROP TABLE channels')
                c.execute('DROP TABLE programs')
                c.execute('CREATE TABLE channels(id TEXT, title TEXT, logo TEXT, stream_url TEXT, source TEXT, visible BOOLEAN, weight INTEGER, PRIMARY KEY (id, source), FOREIGN KEY(source) REFERENCES sources(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED)')
                c.execute('CREATE TABLE programs(channel TEXT, title TEXT, start_date TIMESTAMP, end_date TIMESTAMP, description TEXT, image_large TEXT, image_small TEXT, source TEXT, updates_id INTEGER, FOREIGN KEY(channel, source) REFERENCES channels(id, source) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED, FOREIGN KEY(updates_id) REFERENCES updates(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED)')
                c.execute('CREATE INDEX program_list_idx ON programs(source, channel, start_date, end_date)')
                c.execute('CREATE INDEX start_date_idx ON programs(start_date)')
                c.execute('CREATE INDEX end_date_idx ON programs(end_date)')

            # make sure we have a record in sources for this Source
            c.execute("INSERT OR IGNORE INTO sources(id, channels_updated) VALUES(?, ?)", [self.KEY, datetime.datetime.fromtimestamp(0)])

            self.conn.commit()
            c.close()

        except sqlite3.OperationalError, ex:
            raise DatabaseSchemaException(ex)

class DrDkSource(Source):
    KEY = 'drdk'
    CHANNELS_URL = 'http://www.dr.dk/tjenester/programoversigt/dbservice.ashx/getChannels?type=tv'
    PROGRAMS_URL = 'http://www.dr.dk/tjenester/programoversigt/dbservice.ashx/getSchedule?channel_source_url=%s&broadcastDate=%s'

    def __init__(self, addon, cachePath):
        super(DrDkSource, self).__init__(addon, cachePath)

    def getDataFromExternal(self, date, progress_callback = None):
        jsonChannels = simplejson.loads(self._downloadUrl(self.CHANNELS_URL))

        channels = jsonChannels['result']
        for idx, channel in enumerate(channels):
            c = Channel(id = channel['source_url'], title = channel['name'])
            yield c

            url = self.PROGRAMS_URL % (channel['source_url'].replace('+', '%2b'), date.strftime('%Y-%m-%dT00:00:00'))
            jsonPrograms = simplejson.loads(self._downloadUrl(url))
            for program in jsonPrograms['result']:
                if program.has_key('ppu_description'):
                    description = program['ppu_description']
                else:
                    description = strings(NO_DESCRIPTION)

                p = Program(c, program['pro_title'], self._parseDate(program['pg_start']), self._parseDate(program['pg_stop']), description)
                yield p

            if progress_callback:
                if not progress_callback(100.0 / len(channels) * idx):
                    raise SourceUpdateCanceledException()

    def _parseDate(self, dateString):
        t = time.strptime(dateString[:19], '%Y-%m-%dT%H:%M:%S')
        return datetime.datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


class YouSeeTvSource(Source):
    KEY = 'youseetv'

    def __init__(self, addon, cachePath):
        super(YouSeeTvSource, self).__init__(addon, cachePath)
        self.date = datetime.datetime.today()
        self.channelCategory = addon.getSetting('youseetv.category')
        self.ysApi = ysapi.YouSeeTVGuideApi()

    def getDataFromExternal(self, date, progress_callback = None):
        channels = self.ysApi.channelsInCategory(self.channelCategory)
        for idx, channel in enumerate(channels):
            c = Channel(id = channel['id'], title = channel['name'], logo = channel['logo'])
            yield c

            for program in self.ysApi.programs(c.id, tvdate = date):
                description = program['description']
                if description is None:
                    description = strings(NO_DESCRIPTION)

                imagePrefix = program['imageprefix']

                p = Program(
                    c,
                    program['title'],
                    self._parseDate(program['begin']),
                    self._parseDate(program['end']),
                    description,
                    imagePrefix + program['images_sixteenbynine']['large'],
                    imagePrefix + program['images_sixteenbynine']['small'],
                )
                yield p


            if progress_callback:
                if not progress_callback(100.0 / len(channels) * idx):
                    raise SourceUpdateCanceledException()

    def _parseDate(self, dateString):
        return datetime.datetime.fromtimestamp(dateString)


class XMLTVSource(Source):
    KEY = 'xmltv'

    def __init__(self, addon, cachePath):
        super(XMLTVSource, self).__init__(addon, cachePath)
        self.logoFolder = addon.getSetting('xmltv.logo.folder')
        self.xmlTvFileLastChecked = datetime.datetime.fromtimestamp(0)
        self.xmltvFile = addon.getSetting('xmltv.file')

        if not self.xmltvFile or not xbmcvfs.exists(self.xmltvFile):
            raise SourceNotConfiguredException()

        tempFile = os.path.join(self.cachePath, '%s.xmltv.tmp' % self.KEY)
        xbmc.log('[script.tvguide] Caching XMLTV file...')
        xbmcvfs.copy(addon.getSetting('xmltv.file'), tempFile)

        if not os.path.exists(tempFile):
            raise SourceException('XML TV file was not cached, does it exist?')

        # if xmlTvFile doesn't exists or the file size is different from tempFile
        # we copy the tempFile to xmlTvFile which in turn triggers a reload in self._isChannelListCacheExpired(..)
        if not os.path.exists(self.xmlTvFile) or os.path.getsize(self.xmlTvFile) != os.path.getsize(tempFile):
            if os.path.exists(self.xmlTvFile):
                os.unlink(self.xmlTvFile)
            os.rename(tempFile, self.xmlTvFile)

    def getDataFromExternal(self, date, progress_callback = None):
        size = os.path.getsize(self.xmlTvFile)
        f = open(self.xmlTvFile, "rb")
        context = ElementTree.iterparse(f, events=("start", "end"))
        return parseXMLTV(context, f, size, self.logoFolder, progress_callback)

    def _isChannelListCacheExpired(self):
        """
        Check if xmlTvFile was modified, otherwise cache is not expired.
        Only check filesystem once every 5 minutes
        """
        delta = datetime.datetime.now() - self.xmlTvFileLastChecked
        if delta.seconds < 300:
            return False

        try:
            c = self.conn.cursor()
            c.execute('SELECT channels_updated FROM sources WHERE id=?', [self.KEY])
            row = c.fetchone()
            if not row:
                return True
            lastUpdated = row['channels_updated']
            c.close()
        except TypeError:
            return True
        
        fileModified = datetime.datetime.fromtimestamp(os.path.getmtime(self.xmlTvFile))
        return fileModified > lastUpdated

    def _isProgramListCacheExpired(self, date = datetime.datetime.now()):
        return self._isChannelListCacheExpired()

class XMLTVWEBSource(Source):
    KEY = 'xmltv-url'

    def __init__(self, addon, cachePath):
        
        def extract_file(path):
            if path.endswith('.zip'):
                opener, mode = zipfile.ZipFile, 'r'
            elif path.endswith('.tar.gz') or path.endswith('.tgz'):
                opener, mode = tarfile.open, 'r:gz'
            elif path.endswith('.tar.bz2') or path.endswith('.tbz'):
                opener, mode = tarfile.open, 'r:bz2'
            else: 
                raise ValueError, "Could not extract `%s` as no appropriate extractor is found" % path
            xbmc.log('[script.tvguide] opener: ' + str(opener), xbmc.LOGDEBUG)
            cwd = os.getcwd()
            os.chdir(self.cachePath)
           
            try:
                file = opener(path, mode)
                try: file.extractall()
                except Exception as ex:
                    raise SourceException('Exception! ' + str(ex))
                finally: file.close()
            except Exception as ex:
                raise SourceException('Exception! ' + str(ex))
            finally:
                os.chdir(cwd)
        
        
        
        xbmc.log('[script.tvguide] Entering Init', xbmc.LOGDEBUG)        
        super(XMLTVWEBSource, self).__init__(addon, cachePath)
        self.logoFolder = addon.getSetting('xmltv.logo.folder')
        self.xmlTvFileLastChecked = datetime.datetime.fromtimestamp(0)
        self.HTMLURL = addon.getSetting('xmltv.url')

        if not addon.getSetting('xmltv.url'):
            raise SourceNotConfiguredException()
        
        self.xmlTvFile = os.path.join(self.cachePath, '%s.xmltv' % self.KEY) 
        self.xmltvguide = os.path.join(self.cachePath, "guide.xml")   
        tempFile = os.path.join(self.cachePath, '%s.xmltv.tmp' % self.KEY)
        
        
        if (os.path.exists(self.xmltvguide) and ((datetime.datetime.fromtimestamp(os.path.getmtime(self.xmltvguide)) - datetime.datetime.now()).days > 1)) or not os.path.exists(self.xmltvguide):
        # if downloaded xmltv file is older than 1 day or is not present then download it
            
            xbmc.log('[script.tvguide] Obtaining new XMLTV file from URL', xbmc.LOGDEBUG)
            opener = urllib2.build_opener(urllib2.HTTPHandler(debuglevel=1))
            opener.addheaders = [('User-agent', 'Mozilla/5.0')]
            response = opener.open(self.HTMLURL)
            myparser = MyHTMLParser(response)
            xbmc.log('[script.tvguide] Parsing XMLTV location webpage...')
            self.XMLTVURL = myparser.link
            xbmc.log('[script.tvguide] XMLTV new URL:' + self.XMLTVURL, xbmc.LOGDEBUG)
            self.downloadedTarName = self.XMLTVURL.split('/')[-1]
            downloadedTarPath = os.path.join(self.cachePath, self.downloadedTarName)
            
            try:
                r = urllib2.Request(self.XMLTVURL)
                f = urllib2.urlopen(r)
                xbmc.log('[script.tvguide] Downloading: ' + self.XMLTVURL, xbmc.LOGDEBUG)
                xbmc.log('[script.tvguide] downloadedTarName: ' + self.downloadedTarName, xbmc.LOGDEBUG)
                xbmc.log('[script.tvguide] downloadedTarPath: ' + downloadedTarPath, xbmc.LOGDEBUG)
                local_file = open(downloadedTarPath, "wb")
                xbmc.log('[script.tvguide] local_file: ' + str(local_file), xbmc.LOGDEBUG)
                xbmc.log('[script.tvguide] Writing: ' + downloadedTarPath, xbmc.LOGDEBUG)
                local_file.write(f.read())
                local_file.close()    
                f.close()

            except urllib2.URLError, e:
                raise SourceException('Failed to fetch file: ' + str(e))
            except IOError as e:
                raise SourceException("I/O error({0}): {1}" + str(e.errno) + str(e.strerror))
            except Exception as ex:
                raise SourceException('Problem downloading file!)' + str (ex))

            extract_file(downloadedTarPath)
            xbmc.log('[script.tvguide] xmltvfile: ' + self.xmltvguide, xbmc.LOGDEBUG)
            if not os.path.exists(self.xmltvguide):
                raise SourceException('XML TV file from TAR does not exist!')
                
            xbmcvfs.copy(self.xmltvguide, self.xmlTvFile)    
            xbmc.log('[script.tvguide] Caching XMLTV file...')
            xbmcvfs.copy(self.xmlTvFile, tempFile)

        if not os.path.exists(tempFile):
            raise SourceException('XML TV file was not cached, does it exist?')

        # if xmlTvFile doesn't exists or the file size is different from tempFile
        # we copy the tempFile to xmlTvFile which in turn triggers a reload in self._isChannelListCacheExpired(..)
        if not os.path.exists(self.xmlTvFile) or os.path.getsize(self.xmlTvFile) != os.path.getsize(tempFile):
            if os.path.exists(self.xmlTvFile):
                os.unlink(self.xmlTvFile)
            os.rename(tempFile, self.xmlTvFile)

    
            
    def getDataFromExternal(self, date, progress_callback = None):
        size = os.path.getsize(self.xmlTvFile)
        f = open(self.xmlTvFile, "rb")
        context = ElementTree.iterparse(f, events=("start", "end"))
        return parseXMLTV(context, f, size, self.logoFolder, progress_callback)

    def _isChannelListCacheExpired(self):
        """
        Check if xmlTvFile was modified, otherwise cache is not expired.
        Only check filesystem once every 5 minutes
        """
        delta = datetime.datetime.now() - self.xmlTvFileLastChecked
        if delta.seconds < 300:
            return False

        try:
            c = self.conn.cursor()
            c.execute('SELECT channels_updated FROM sources WHERE id=?', [self.KEY])
            row = c.fetchone()
            if not row:
                return True
            lastUpdated = row['channels_updated']
            c.close()
        except TypeError:
            return True

        fileModified = datetime.datetime.fromtimestamp(os.path.getmtime(self.xmlTvFile))
        return fileModified > lastUpdated

    def _isProgramListCacheExpired(self, startTime):
        return self._isChannelListCacheExpired()
        
        
class ONTVSource(Source):
    KEY = 'ontv'

    def __init__(self, addon, cachePath):
        super(ONTVSource, self).__init__(addon, cachePath)
        self.ontvUrl = addon.getSetting('ontv.url')

    def getDataFromExternal(self, date, progress_callback = None):
        xml = self._downloadUrl(self.ontvUrl)
        io = StringIO.StringIO(xml)
        context = ElementTree.iterparse(io)
        return parseXMLTV(context, io, len(xml), None, progress_callback)

    def _isProgramListCacheExpired(self, date = datetime.datetime.now()):
        return self._isChannelListCacheExpired()


class JSONSource(Source):
    KEY = 'json-url'

    def __init__(self, addon, cachePath):
        super(JSONSource, self).__init__(addon, cachePath)
        self.playbackUsingWeebTv = False
        self.JSONURL = addon.getSetting('json.url')

        if not addon.getSetting('json.url'):
            raise SourceNotConfiguredException()
                    
        try:
            if addon.getSetting('weebtv.playback') == 'true':
                xbmcaddon.Addon(id = 'plugin.video.weeb.tv') # raises Exception if addon is not installed
                self.playbackUsingWeebTv = True
        except Exception:
            ADDON.setSetting('weebtv.playback', 'false')
            xbmcgui.Dialog().ok(ADDON.getAddonInfo('name'), strings(WEEBTV_WEBTV_MISSING_1),
                strings(WEEBTV_WEBTV_MISSING_2), strings(WEEBTV_WEBTV_MISSING_3))

    def getDataFromExternal(self, date, progress_callback = None):
        url = self.JSONURL + '?d=' + date.strftime('%Y-%m-%d')
        print 'Load JSON URL: ' + url
        try:
            r = urllib2.Request(url)
            u = urllib2.urlopen(r)
            json = u.read()
            u.close()

            channels = simplejson.loads(json)
        except urllib2.URLError, e:
            raise SourceException('Failed to fetch JSON: ' + str(e))
        except Exception:
            raise SourceException('Invalid JSON source (failed to parse output)')
        
        
        for idx, ch in enumerate(channels):
                try:
                 print 'Parsing channel: ' + ch['n'].encode("utf-8","ignore")
                except KeyError:
                 print ch
            #try:           
                if ch.has_key('l') and ch['l'] is not None: # Channel logo
                    ch['l'] = str(ch['l'])
                else:
                    ch['l'] = None
                
                c = Channel(id = ch['i'], title = ch['n'], logo = ch['l'])
                
                if self.playbackUsingWeebTv and ch.has_key('c') and ch['c'] is not None: # channel numeric id
                    c.streamUrl = 'plugin://plugin.video.weeb.tv/?mode=2&action=1&cid=' + str(ch['c']) + '&title=' + str(ch['n'])
                yield c
                
                print 'Found ' + str(len(ch['p'])) + ' programs'
                if ch.has_key('p') and len(ch['p']) > 0:
                    for pr in ch['p']:
                        #print 'Parsing program: ' + pr['t'].encode("utf-8","ignore")
                        if pr.has_key('d') and pr['d'] is not None: # program description
                            description = pr['d']
                        else:
                            description = strings(NO_DESCRIPTION)

                        if not pr.has_key('l'): # large program image
                            pr['l'] = None
                                                    
                        if not pr.has_key('i'): # small program image aka icon
                            pr['i'] = None
                            
                        p = Program(
                            c,
                            pr['t'],
                            self._parseDate(pr['s']),
                            self._parseDate(pr['e']),
                            description,
                            pr['l'],
                            pr['i']
                        )
                        yield p
            
           
                    if progress_callback:
                        if not progress_callback(100.0 / len(channels) * idx):
                            raise SourceUpdateCanceledException()
            #except Exception:
            #    raise SourceException('External JSON looks invalid, error detected in element ' + str(idx))
              
    def _parseDate(self, dateString):
        return datetime.datetime.fromtimestamp(dateString)

def parseXMLTVDate(dateString):
    if dateString is not None:
        if dateString.find(' ') != -1:
            # remove timezone information
            dateString = dateString[:dateString.find(' ')]
        t = time.strptime(dateString, '%Y%m%d%H%M%S')
        return datetime.datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    else:
        return None

def parseXMLTV(context, f, size, logoFolder, progress_callback):
    event, root = context.next()
    elements_parsed = 0

    for event, elem in context:
        if event == "end":
            result = None
            if elem.tag == "programme":
                channel = elem.get("channel")
                description = elem.findtext("desc")
                iconElement = elem.find("icon")
                icon = None
                if iconElement is not None:
                    icon = iconElement.get("src")
                if not description:
                    description = strings(NO_DESCRIPTION)
                title = elem.findtext('title')
		subtitle = elem.findtext('sub-title')
		if subtitle:
			title += ": " + elem.findtext('sub-title')
                result = Program(channel, title, parseXMLTVDate(elem.get('start')), parseXMLTVDate(elem.get('stop')), description, imageSmall=icon)

            elif elem.tag == "channel":
                id = elem.get("id")
                title = elem.findtext("display-name")
                logo = None
                if logoFolder:
                    logoFile = os.path.join(logoFolder.encode('utf-8', 'ignore'), title.encode('utf-8', 'ignore') + '.png')
                    if xbmcvfs.exists(logoFile):
                        logo = logoFile
                if not logo:
                    iconElement = elem.find("icon")
                    if iconElement is not None:
                        logo = iconElement.get("src")
                result = Channel(id, title, logo)

            if result:
                elements_parsed += 1
                if progress_callback and elements_parsed % 500 == 0:
                    if not progress_callback(100.0 / size * f.tell()):
                        raise SourceUpdateCanceledException()
                yield result

        root.clear()
    f.close()

class FileWrapper(object):
    def __init__(self, filename):
        self.vfsfile = xbmcvfs.File(filename)
        self.bytesRead = 0

    def close(self):
        self.vfsfile.close()

    def read(self, bytes):
        self.bytesRead += bytes
        return self.vfsfile.read(bytes)

    def size(self):
        return self.vfsfile.size()

    def tell(self):
        return self.bytesRead



def instantiateSource(addon):
    SOURCES = {
        'YouSee.tv' : YouSeeTvSource,
        'DR.dk' : DrDkSource,
        'XMLTV' : XMLTVSource,
        'ONTV.dk' : ONTVSource,
        'JSON-URL' : JSONSource,
        'XMLTV-URL' : XMLTVWEBSource
    }

    cachePath = xbmc.translatePath(ADDON.getAddonInfo('profile'))

    if not os.path.exists(cachePath):
        os.makedirs(cachePath)

    try:
        activeSource = SOURCES[addon.getSetting('source')]
    except KeyError:
        activeSource = SOURCES['YouSee.tv']

    return activeSource(addon, cachePath)


