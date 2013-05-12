#!/usr/bin/env python
import os
import sys
import pwd
import shlex
import subprocess
from threading import Thread
from Queue import Queue, Empty
from xml.dom import pulldom


class NmapProcess:
    """
    NmapProcess is a class which wraps around the nmap executable.
    Consequently, in order to run an NmapProcess, nmap should be installed
    on the host running the script. By default NmapProcess will produce
    the output of the nmap scan in the nmap XML format. This could be then
    parsed out via the NmapParser class from libnmap.parser module.
    """
    def __init__(self, targets="127.0.0.1",
                 options="-sT", event_callback=None, safe_mode=True):
        """
        Constructor of NmapProcess class.

        :param targets: hosts to be scanned. Could be a string of hosts
        separated with a coma or a python list of hosts/ip.
        :type targets: string or list

        :param options: list of nmap options to be applied to scan.
        These options are all documented in nmap's man pages.

        :param event_callback: callable function which will be ran
        each time nmap process outputs data. This function will receive
        two parameters:
            1. the nmap process object
            2. the data produced by nmap process. See readme for examples.

        :param safe_mode: parameter to protect unsafe options like -oN, -oG,
        -iL, -oA,...

        :return: NmapProcess object

        """
        self.__nmap_proc = None
        self.__nmap_rc = 0

        unsafe_opts = set(['-oG', '-oN', '-iL', '-oA', '-oS', '-oX',
                           '--iflist', '--resume', '--stylesheet',
                           '--datadir'])

        self.__nmap_binary_name = "nmap"
        self.__nmap_fixed_options = "-oX - -vvv --stats-every 2s"
        self.__nmap_binary = self._whereis(self.__nmap_binary_name)
        if self.__nmap_binary is None:
            raise EnvironmentError(1, "nmap is not installed or could "
                                      "not be found in system path")

        self.__sudo_run = ""
        if isinstance(targets, str):
            self.__nmap_targets = targets.replace(" ", "").split(',')
        elif isinstance(targets, list):
            self.__nmap_targets = targets
        else:
            raise Exception("Supplied target list should be either a "
                            "string of a list")

        optlist = set(options.split())
        if safe_mode and not optlist.isdisjoint(unsafe_opts):
            raise Exception("unsafe options activated while safe_mode "
                            "is set True")

        self.__nmap_dynamic_options = options

        self.__nmap_command_line = self.get_command_line()
        if event_callback and callable(event_callback):
            self._nmap_event_callback = event_callback
        else:
            self._nmap_event_callback = None
        (self.DONE, self.READY, self.RUNNING,
         self.CANCELLED, self.FAILED) = range(5)

        # API usable in callback function
        self.state = self.READY
        self.starttime = 0
        self.endtime = 0
        self.__progress = 0
        self.__etc = 0
        self.elapsed = ''
        self.summary = ''
        self.stdout = ''
        self.stderr = ''

    def _run_init(self):
        """
        Protected method ran at every call to run(). This ensures that no
        no parameters are polluted.
        """
        self.__nmap_proc = None
        self.__nmap_rc = -1
        self.__nmap_command_line = self.get_command_line()
        self.state = self.READY
        self.__progress = 0
        self.__etc = 0
        self.starttime = 0
        self.endtime = 0
        self.elapsed = ''
        self.summary = ''
        self.nmap_version = ''
        self.__io_queue = Queue()
        self.__ioerr_queue = Queue()
        self.stdout = ''
        self.stderr = ''

    def _whereis(self, program):
        """
        Protected method enabling the object to find the full path of a binary
        from its PATH environment variable.

        :param program: name of a binary for which the full path needs to
        be discovered.

        :return: the full path to the binary.

        :todo: add a default path list in case PATH is empty.
        """
        for path in os.environ.get('PATH', '').split(':'):
            if (os.path.exists(os.path.join(path, program)) and not
                    os.path.isdir(os.path.join(path, program))):
                return os.path.join(path, program)
        return None

    def get_command_line(self):
        """
        Public method returning the reconstructed command line ran via the lib

        :return: the full nmap command line to run
        :rtype: string
        """
        return ("%s %s %s %s %s".lstrip() % (self.__sudo_run,
                                             self.__nmap_binary,
                                             self.__nmap_fixed_options,
                                             self.__nmap_dynamic_options,
                                             " ".join(self.__nmap_targets)))

    def sudo_run(self, run_as='root'):
        """
        Public method enabling the library's user to run the scan with
        priviledges via sudo. The sudo configuration should be set manually
        on the local system otherwise sudo will prompt for a password.
        This method alters the command line by prefixing the sudo command to
        nmap and will then call self.run()

        :param run_as: user name to which the lib needs to sudo to run the scan

        :return: return code from nmap execution
        """
        sudo_user = run_as.split().pop()
        try:
            pwd.getpwnam(sudo_user).pw_uid
        except KeyError:
            raise

        sudo_path = self._whereis("sudo")
        if sudo_path is None:
            raise EnvironmentError(2, "sudo is not installed or "
                                      "could not be found in system path: "
                                      "cannot run nmap with sudo")

        self.__sudo_run = "%s -u %s" % (sudo_path, sudo_user)
        rc = self.run()
        self.__sudo_run = ""

        return rc

    def run(self):
        """
        Public method which is usually called right after the constructor
        of NmapProcess. This method starts the nmap executable's subprocess.
        It will also bind to threads that will read from subprocess' stdout
        and stderr and push the lines read in a python queue for futher
        processing.

        return: return code from nmap execution from self.__wait()
        """
        def stream_reader(thread_stdout, io_queue):
            """
            local function that will read lines from a file descriptor
            and put the data in a python queue for futher processing.

            :param thread_stdout: file descriptor to read lines from.
            :param io_queue: queue in which read lines will be pushed.
            """
            for streamline in iter(thread_stdout.readline, b''):
                try:
                    if streamline is not None:
                        io_queue.put(streamline)
                except Queue.Full:
                    raise Exception("Queue ran out of buffer: "
                                    "increase q.get(timeout) value")

        self._run_init()
        try:
            _tmp_cmdline = shlex.split(self.__nmap_command_line)
            self.__nmap_proc = subprocess.Popen(args=_tmp_cmdline,
                                                stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE)
            tout = Thread(target=stream_reader, name='stdout-reader',
                          args=(self.__nmap_proc.stdout, self.__io_queue))
            terr = Thread(target=stream_reader, name='stderr-reader',
                          args=(self.__nmap_proc.stderr, self.__ioerr_queue))
            tout.start()
            terr.start()
            self.state = self.RUNNING
        except OSError:
            self.state = self.FAILED
            raise

        return self.__wait()

    def __wait(self):
        """
        Private method, called by run() which will loop and
        process the data from the python queues. Those queues are fed by
        the stream_readers of run, reading lines from subprocess.stdout/err.
        Each time data is pushed in the nmap's stdout queue:
        1. __process_event is called with the acquired data as input
        2. if a event callback was provided, the function passed in the
           constructor is called.

        :return: return code from nmap execution
        """
        thread_stream = ''
        while self.__nmap_proc.poll() is None or not self.__io_queue.empty():
            try:
                thread_stream = self.__io_queue.get(timeout=1)
            except Empty:
                pass
            except KeyboardInterrupt:
                break
            else:
                e = self.__process_event(thread_stream)
                if self._nmap_event_callback and e:
                    self._nmap_event_callback(self, thread_stream)
                self.stdout += thread_stream

        self.__nmap_rc = self.__nmap_proc.poll()
        if self.rc is None:
            self.state = self.CANCELLED
        elif self.rc == 0:
            self.state = self.DONE
            self.__progress = 100
        else:
            self.state = self.FAILED
            self.stderr = self.__ioerr_queue.get(timeout=2)

        return self.rc

    def is_running(self):
        """
        Checks if nmap is still running.

        :return: True if nmap is still running
        """
        return self.state == self.RUNNING

    def has_terminated(self):
        """
        Checks if nmap has terminated. Could have failed or succeeded

        :return: True if nmap process is not running anymore.
        """
        return (self.state == self.DONE or self.state == self.FAILED
                or self.state == self.CANCELLED)

    def has_failed(self):
        """
        Checks if nmap has failed.

        :return: True if nmap process errored.
        """
        return self.state == self.FAILED

    def is_successful(self):
        """
        Checks if nmap terminated successfully.

        :return: True if nmap terminated successfully.
        """
        return self.state == self.DONE

    def __process_event(self, eventdata):
        """
        Private method called while nmap process is running. It enables the
        library to handle specific data/events produced by nmap process.
        So far, the following events are supported:
        1. task progress: updates estimated time to completion and percentage
           done while scan is running. Could be used in combination with a
           callback function which could then handle this data while scan is
           running.
        2. nmap run: header of the scan. Usually displayed when nmap is started
        3. finished: when nmap scan ends.

        :return: True is event is known.
        """
        rval = False
        try:
            edomdoc = pulldom.parseString(eventdata)
            for e, xmlnode in edomdoc:
                if e is not None and e == pulldom.START_ELEMENT:
                    if (xmlnode.nodeName == 'taskprogress' and
                            xmlnode.attributes.keys()):
                        percent_done = xmlnode.attributes['percent'].value
                        etc_done = xmlnode.attributes['etc'].value
                        self.__progress = percent_done
                        self.__etc = etc_done
                        rval = True
                    elif (xmlnode.nodeName == 'nmaprun' and
                            xmlnode.attributes.keys()):
                        self.starttime = xmlnode.attributes['start'].value
                        self.nmap_version = xmlnode.attributes['version'].value
                        rval = True
                    elif (xmlnode.nodeName == 'finished' and
                            xmlnode.attributes.keys()):
                        self.endtime = xmlnode.attributes['time'].value
                        self.elapsed = xmlnode.attributes['elapsed'].value
                        self.summary = xmlnode.attributes['summary'].value
                        rval = True
        except:
            pass
        return rval

    @property
    def rc(self):
        """
        Accessor for nmap execution's return code

        :return: nmap execution's return code
        """
        return self.__nmap_rc

    @property
    def etc(self):
        """
        Accessor for estimated time to completion

        :return:  estimated time to completion
        """
        return self.__etc

    @property
    def progress(self):
        """
        Accessor for progress status in percentage

        :return: percentage of job processed.
        """
        return self.__progress


def main(argv):
    def mycallback(nmapscan=None, data=""):
        if nmapscan.is_running():
            print "Progress: %s %% - ETC: %s" % (nmapscan.progress,
                                                 nmapscan.etc)

    nm = NmapProcess("localhost", options="-sT", event_callback=mycallback)
    rc = nm.run()

    if rc == 0:
        print "Scan started {0} {1}".format(nm.starttime, nm.nmap_version)
        print "results size: {0}".format(len(nm.stdout))
        print "Scan ended {0}: {1}".format(nm.endtime, nm.summary)
        print "state: %s" % nm.state
    else:
        print "Error: {stderr}".format(stderr=nm.stderr)
        print "Result: {0}".format(nm.stdout)

if __name__ == '__main__':
    main(sys.argv[1:])