"""
Utilities for reading descriptors from local directories and archives. This is
mostly done through the DescriptorReader class, which is an iterator for the
descriptor data in a series of destinations. For example...

  my_descriptors = [
    "/tmp/server-descriptors-2012-03.tar.bz2",
    "/tmp/archived_descriptors/",
  ]
  
  reader = DescriptorReader(my_descriptors)
  
  # prints the contents of all the descriptor files
  with reader:
    for descriptor in reader:
      print descriptor

This ignores files that cannot be processed due to read errors or unparsable
content. To be notified of skipped files you can register a listener with
register_skip_listener().

The DescriptorReader keeps track of the last modified timestamps for descriptor
files that it has read so it can skip unchanged files if ran again. This
listing of processed files can also be persisted and applied to other
DescriptorReaders. For instance, the following prints descriptors as they're
changed over the course of a minute, and picks up where it left off if ran
again...

  reader = DescriptorReader(["/tmp/descriptor_data"])
  
  try:
    processed_files = load_processed_files("/tmp/used_descriptors")
    reader.set_processed_files(processed_files)
  except: pass # could not load, mabye this is the first run
  
  start_time = time.time()
  
  while time.time() - start_time < 60:
    # prints any descriptors that have changed since last checked
    with reader:
      for descriptor in reader:
        print descriptor
    
    time.sleep(1)
  
  save_processed_files(reader.get_processed_files(), "/tmp/used_descriptors")


load_processed_files - Loads a listing of processed files.
save_processed_files - Saves a listing of processed files.

DescriptorReader - Iterator for descriptor data on the local file system.
  |- get_processed_files - provides the listing of files that we've processed
  |- set_processed_files - sets our tracking of the files we have processed
  |- register_skip_listener - adds a listener that's notified of skipped files
  |- start - begins reading descriptor data
  |- stop - stops reading descriptor data
  |- __enter__ / __exit__ - manages the descriptor reader thread in the context
  +- __iter__ - iterates over descriptor data in unread files

FileSkipped - Base exception for a file that was skipped.
  |- ParsingFailure - Contents can't be parsed as descriptor data.
  |- UnrecognizedType - File extension indicates non-descriptor data.
  +- ReadFailed - Wraps an error that was raised while reading the file.
     +- FileMissing - File does not exist.
"""

import os
import threading
import mimetypes
import Queue

# TODO: Remianing impementation items...
# - implement gzip and bz2 reading
# - maximum read-ahead

# Maximum number of descriptors that we'll read ahead before waiting for our
# caller to fetch some of them. This is included to avoid unbounded memory
# usage. This condition will be removed if set to zero.

MAX_STORED_DESCRIPTORS = 20

# flag to indicate when the reader thread is out of descriptor files to read
FINISHED = "DONE"

class FileSkipped(Exception):
  "Base error when we can't provide descriptor data from a file."

class ParsingFailure(FileSkipped):
  "File contents could not be parsed as descriptor data."
  
  def __init__(self, parsing_exception):
    self.exception = parsing_exception

class UnrecognizedType(FileSkipped):
  "File's mime type indicates that it isn't descriptor data."
  
  def __init__(self, mime_type):
    self.mime_type = mime_type

class ReadFailed(FileSkipped):
  "An IOError occured while trying to read the file."
  
  def __init__(self, read_exception):
    self.exception = read_exception

class FileMissing(ReadFailed):
  "File does not exist."
  
  def __init__(self):
    ReadFailed.__init__(self, None)

def load_processed_files(path):
  """
  Loads a dictionary of 'path => last modified timestamp' mappings, as
  persisted by save_processed_files(), from a file.
  
  Arguments:
    path (str) - location to load the processed files dictionary from
  
  Returns:
    dict of 'path (str) => last modified unix timestamp (int)' mappings
  
  Raises:
    IOError if unable to read the file
    TypeError if unable to parse the file's contents
  """
  
  processed_files = {}
  
  with open(path) as input_file:
    for line in input_file.readlines():
      line = line.strip()
      
      if not line: continue # skip blank lines
      
      if not " " in line:
        raise TypeError("Malformed line: %s" % line)
      
      path, timestamp = line.rsplit(" ", 1)
      
      if not os.path.isabs(path):
        raise TypeError("'%s' is not an absolute path" % path)
      elif not timestamp.isdigit():
        raise TypeError("'%s' is not an integer timestamp" % timestamp)
      
      processed_files[path] = int(timestamp)
  
  return processed_files

def save_processed_files(processed_files, path):
  """
  Persists a dictionary of 'path => last modified timestamp' mappings (as
  provided by the DescriptorReader's get_processed_files() method) so that they
  can be loaded later and applied to another DescriptorReader.
  
  Arguments:
    processed_files (dict) - 'path => last modified' mappings
    path (str)             - location to save the processed files dictionary to
  
  Raises:
    IOError if unable to write to the file
    TypeError if processed_files is of the wrong type
  """
  
  # makes the parent directory if it doesn't already exist
  try:
    path_dir = os.path.dirname(path)
    if not os.path.exists(path_dir): os.makedirs(path_dir)
  except OSError, exc: raise IOError(exc)
  
  with open(path, "w") as output_file:
    for path, timestamp in processed_files.items():
      if not os.path.isabs(path):
        raise TypeError("Only absolute paths are acceptable: %s" % path)
      
      output_file.write("%s %i\n" % (path, timestamp))

class DescriptorReader:
  """
  Iterator for the descriptor data on the local file system. This can process
  text files, tarball archives (gzip or bzip2), or recurse directories.
  
  Arguments:
    targets (list)      - paths for files or directories to be read from
    follow_links (bool) - determines if we'll follow symlinks when transversing
                          directories
  """
  
  def __init__(self, targets, follow_links = False):
    self._targets = targets
    self._follow_links = follow_links
    self._skip_listeners = []
    self._processed_files = {}
    
    self._reader_thread = None
    self._reader_thread_lock = threading.RLock()
    
    self._iter_lock = threading.RLock()
    self._iter_notice = threading.Event()
    
    self._is_stopped = threading.Event()
    self._is_stopped.set()
    
    # Descriptors that we have read but not yet provided to the caller. A
    # FINISHED entry is used by the reading thread to indicate the end.
    
    self._unreturned_descriptors = Queue.Queue()
  
  def get_processed_files(self):
    """
    For each file that we have read descriptor data from this provides a
    mapping of the form...
    
    absolute path (str) => last modified unix timestamp (int)
    
    This includes entries set through the set_processed_files() method.
    
    Returns:
      dict with the absolute paths and unix timestamp for the last modified
      times of the files we have processed
    """
    
    # make sure that we only provide back absolute paths
    return dict((os.path.abspath(k), v) for (k, v) in self._processed_files.items())
  
  def set_processed_files(self, processed_files):
    """
    Sets the listing of the files we have processed. Most often this is useful
    as a method for pre-populating the listing of descriptor files that we have
    seen.
    
    Arguments:
      processed_files (dict) - mapping of absolute paths (str) to unix
                               timestamps for the last modified time (int)
    """
    
    self._processed_files = dict(processed_files)
  
  def register_skip_listener(self, listener):
    """
    Registers a listener for files that are skipped. This listener is expected
    to be a functor of the form...
    
    my_listener(path, exception)
    
    Arguments:
      listener (functor) - functor to be notified of files that are skipped to
                           read errors or because they couldn't be parsed as
                           valid descriptor data
    """
    
    self._skip_listeners.append(listener)
  
  def start(self):
    """
    Starts reading our descriptor files.
    
    Raises:
      ValueError if we're already reading the descriptor files
    """
    
    with self._reader_thread_lock:
      if self._reader_thread:
        raise ValueError("Already running, you need to call stop() first")
      else:
        self._is_stopped.clear()
        self._reader_thread = threading.Thread(target = self._read_descriptor_files, name="Descriptor Reader")
        self._reader_thread.setDaemon(True)
        self._reader_thread.start()
  
  def stop(self):
    """
    Stops further reading of descriptor files.
    """
    
    with self._reader_thread_lock:
      self._is_stopped.set()
      self._iter_notice.set()
      self._reader_thread.join()
      self._reader_thread = None
  
  def _read_descriptor_files(self):
    remaining_files = list(self._targets)
    
    while remaining_files and not self._is_stopped.is_set():
      target = remaining_files.pop(0)
      
      if not os.path.exists(target):
        self._notify_skip_listeners(target, FileMissing())
        continue
      
      if os.path.isdir(target):
        # adds all of the files that it contains
        for root, _, files in os.walk(target, followlinks = self._follow_links):
          for filename in files:
            remaining_files.append(os.path.join(root, filename))
          
          # this can take a while if, say, we're including the root directory
          if self._is_stopped.is_set(): break
      else:
        # This is a file. Register it's last modified timestamp and check if
        # it's a file that we should skip.
        
        last_modified = os.stat(target).st_mtime
        last_used = self._processed_files.get(target)
        
        if last_used and last_used >= last_modified:
          continue
        else:
          self._processed_files[target] = last_modified
        
        # The mimetypes module only checks the file extension. To actually
        # check the content (like the 'file' command) we'd need something like
        # pymagic (https://github.com/cloudburst/pymagic).
        
        target_type = mimetypes.guess_type(target)
        
        if target_type[0] in (None, 'text/plain'):
          # either '.txt' or an unknown type
          self._handle_descriptor_file(target)
        elif target_type == ('application/x-tar', 'gzip'):
          self._handle_archive_gzip(target)
        elif target_type == ('application/x-tar', 'bzip2'):
          self._handle_archive_gzip(target)
        else:
          self._notify_skip_listeners(target, UnrecognizedType(target_type))
    
    self._unreturned_descriptors.put(FINISHED)
    self._iter_notice.set()
  
  def __iter__(self):
    with self._iter_lock:
      while not self._is_stopped.is_set():
        try:
          descriptor = self._unreturned_descriptors.get_nowait()
          
          if descriptor == FINISHED: break
          else: yield descriptor
        except Queue.Empty:
          self._iter_notice.wait()
          self._iter_notice.clear()
  
  def _handle_descriptor_file(self, target):
    try:
      # TODO: replace with actual descriptor parsing when we have it
      target_file = open(target)
      self._unreturned_descriptors.put(target_file.read())
      self._iter_notice.set()
    except IOError, exc:
      self._notify_skip_listeners(target, ReadFailed(exc))
  
  def _handle_archive_gzip(self, target):
    pass # TODO: implement
  
  def _handle_archive_bzip(self, target):
    pass # TODO: implement
  
  def _notify_skip_listeners(self, path, exception):
    for listener in self._skip_listeners:
      listener(path, exception)
  
  def __enter__(self):
    self.start()
  
  def __exit__(self, type, value, traceback):
    self.stop()

