#!/usr/bin/env python3
# Consume mail received from PowerMTA
# command-line params may also be present, as per PMTA Users Guide "3.3.12 Pipe Delivery Directives"
#
# Author: Steve Tuck.  (c) 2018 SparkPost
#
# Pre-requisites:
#   pip3 install requests, dnspython
#
import logging, logging.handlers, sys, os, email, time, glob, requests, dns.resolver, smtplib, configparser, random
from html.parser import HTMLParser
# workaround as per https://stackoverflow.com/questions/45124127/unable-to-extract-the-body-of-the-email-file-in-python
from email import policy
from webReporter import Results
from datetime import datetime, timezone

def baseProgName():
    return os.path.basename(sys.argv[0])

def configFileName():
    return os.path.splitext(baseProgName())[0] + '.ini'

def printHelp():
    print('\nNAME')
    print('   ' + baseProgName() + ' [-f file | -d dir]')
    print('   Consume inbound mails, generating opens, clicks, OOBs and FBLs\n')
    print('   Config file {} for must be present in current directory'.format(configFileName()))
    print('')
    print('Parameters')
    print('    (no params)  - ingest a single mail from stdin, e.g. cat mail.msg | src/{}'.format(baseProgName()))
    print('    -f file      - ingest a single mail file in RFC822 format')
    print('    -d directory - look for *.msg files, ingest them, renaming to *.old')
    print('')
    print('Output')
    print('    logfile of actions taken created')

def xstr(s):
    return '' if s is None else str(s)

def timeStr(t):
    utc = datetime.fromtimestamp(t, timezone.utc)
    return datetime.isoformat(utc, sep='T', timespec='seconds')

# -----------------------------------------------------------------------------
# FBL and OOB handling
# -----------------------------------------------------------------------------
ArfFormat = '''From: <{fblFrom}>
Date: Mon, 02 Jan 2006 15:04:05 MST
Subject: FW: Earn money
To: <{fblTo}>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report;
      boundary="{boundary}"

--{boundary}
Content-Type: text/plain; charset="US-ASCII"
Content-Transfer-Encoding: 7bit

This is an email abuse report for an email message
received from IP 10.67.41.167 on Thu, 8 Mar 2005
14:00:00 EDT.
For more information about this format please see
http://www.mipassoc.org/arf/.

--{boundary}
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: SomeGenerator/1.0
Version: 0.1

--{boundary}
Content-Type: message/rfc822
Content-Disposition: inline

From: <{returnPath}>
Received: from mailserver.example.net (mailserver.example.net
        [10.67.41.167])
        by example.com with ESMTP id M63d4137594e46;
        Thu, 08 Mar 2005 14:00:00 -0400
To: <Undisclosed Recipients>
Subject: Earn money
MIME-Version: 1.0
Content-type: text/plain
Message-ID: 8787KJKJ3K4J3K4J3K4J3.mail@{domain}
X-MSFBL: {msfbl}
Date: Thu, 02 Sep 2004 12:31:03 -0500

Spam Spam Spam
Spam Spam Spam
Spam Spam Spam
Spam Spam Spam

--{boundary}--
'''
def buildArf(fblFrom, fblTo, msfbl, returnPath):
    boundary = '_----{0:d}===_61/00-25439-267B0055'.format(int(time.time()))
    domain = fblFrom.split('@')[1]
    msg = ArfFormat.format(fblFrom=fblFrom, fblTo=fblTo, boundary=boundary, returnPath=returnPath, domain=domain, msfbl=msfbl)
    return msg

OobFormat = '''From: {oobFrom}
Date: Mon, 02 Jan 2006 15:04:05 MST
Subject: Returned mail: see transcript for details
Auto-Submitted: auto-generated (failure)
To: {oobTo}
Content-Type: multipart/report; report-type=delivery-status;
	boundary="{boundary}"

This is a MIME-encapsulated message

--{boundary}

The original message was received at Mon, 02 Jan 2006 15:04:05 -0700
from example.com.sink.sparkpostmail.com [52.41.116.105]

   ----- The following addresses had permanent fatal errors -----
<{oobTo}>
    (reason: 550 5.0.0 <{oobTo}>... User unknown)

   ----- Transcript of session follows -----
... while talking to {toDomain}:
>>> DATA
<<< 550 5.0.0 <{oobTo}>... User unknown
550 5.1.1 <{oobTo}>... User unknown
<<< 503 5.0.0 Need RCPT (recipient)

--{boundary}
Content-Type: message/delivery-status

Reporting-MTA: dns; {toDomain}
Received-From-MTA: DNS; {fromDomain}
Arrival-Date: Mon, 02 Jan 2006 15:04:05 MST

Final-Recipient: RFC822; {oobTo}
Action: failed
Status: 5.0.0
Remote-MTA: DNS; {toDomain}
Diagnostic-Code: SMTP; 550 5.0.0 <{oobTo}>... User unknown
Last-Attempt-Date: Mon, 02 Jan 2006 15:04:05 MST

--{boundary}
Content-Type: message/rfc822

{rawMsg}

--{boundary}--
'''
def buildOob(oobFrom, oobTo, rawMsg):
    boundary = '_----{0:d}===_61/00-25439-267B0055'.format(int(time.time()))
    fromDomain = oobFrom.split('@')[1]
    toDomain = oobTo.split('@')[1]
    msg = OobFormat.format(oobFrom=oobFrom, oobTo=oobTo, boundary=boundary, toDomain=toDomain, fromDomain=fromDomain, rawMsg=rawMsg)
    return msg

# Avoid creating backscatter spam https://en.wikipedia.org/wiki/Backscatter_(email). Check that returnPath points to SparkPost.
# If valid, returns the MX and the associated To: addr for FBLs.
def mapRP_MXtoSparkPostFbl(returnPath):
    rpDomainPart = returnPath.split('@')[1]
    try:
        mxList = dns.resolver.query(rpDomainPart, 'MX')             # Will throw exception if not found
        if mxList:
            mx = mxList[0].to_text().split()[1][:-1]                # Take first one in the list, remove the priority field and trailing '.'
            if mx.endswith('smtp.sparkpostmail.com'):               # SparkPost US
                fblTo = 'fbl@sparkpostmail.com'
            elif mx.endswith('e.sparkpost.com'):                    # SparkPost Enterprise
                tenant = mx.split('.')[0]
                fblTo = 'fbl@' + tenant + '.mail.e.sparkpost.com'
            elif mx.endswith('smtp.eu.sparkpostmail.com'):          # SparkPost EU
                fblTo = 'fbl@eu.sparkpostmail.com'
            else:
                return None, None
            return mx, fblTo                                        # Valid
        else:
            return None, None
    except dns.exception.DNSException as err:
        return None, None

def returnPathAddrIn(mail):
    return mail['Return-Path'].lstrip('<').rstrip('>')              # Remove < > brackets from address

# Generate and deliver an FBL response (to cause a spam_complaint event in SparkPost)
# Based on https://github.com/SparkPost/gosparkpost/tree/master/cmd/fblgen
#
def fblGen(mail, shareRes):
    returnPath = returnPathAddrIn(mail)
    if not returnPath:
        shareRes.incrementKey('fbl_missing_return_path')
        return '!Missing Return-Path:'
    elif not mail['to']:
        shareRes.incrementKey('fbl_missing_to')
        return '!Missing To:'
    else:
        fblFrom = mail['to']
        mx, fblTo = mapRP_MXtoSparkPostFbl(returnPath)
        if not mx:
            shareRes.incrementKey('fbl_return_path_not_sparkpost')
            return '!FBL not sent, Return-Path not recognized as SparkPost'
        else:
            arfMsg = buildArf(fblFrom, fblTo, mail['X-MSFBL'], returnPath)
            try:
                # Deliver an FBL to SparkPost using SMTP direct, so that we can check the response code.
                with smtplib.SMTP(mx) as smtpObj:
                    smtpObj.sendmail(fblFrom, fblTo, arfMsg)        # if no exception, the mail is sent (250OK)
                    shareRes.incrementKey('fbl_sent')
                    return 'FBL sent,to ' + fblTo + ' via ' + mx
            except smtplib.SMTPException as err:
                shareRes.incrementKey('fbl_smtp_error')
                return '!FBL endpoint returned SMTP error: ' + str(err)


# Generate and deliver an OOB response (to cause a out_of_band event in SparkPost)
# Based on https://github.com/SparkPost/gosparkpost/tree/master/cmd/oobgen
def oobGen(mail, shareRes):
    returnPath = returnPathAddrIn(mail)
    if not returnPath:
        shareRes.incrementKey('oob_missing_return_path')
        return '!Missing Return-Path:'
    elif not mail['to']:
        shareRes.incrementKey('oob_missing_to')
        return '!Missing To:'
    else:
        mx, _ = mapRP_MXtoSparkPostFbl(returnPath)
        if not mx:
            shareRes.incrementKey('oob_return_path_not_sparkpost')
            return '!OOB not sent, Return-Path not recognized as SparkPost'
        else:
            # OOB is addressed back to the Return-Path: address, from the inbound To: address (i.e. the sink)
            oobTo = returnPath
            oobFrom = str(mail['To'])
            oobMsg = buildOob(oobFrom, oobTo, mail)
            try:
                # Deliver an OOB to SparkPost using SMTP direct, so that we can check the response code.
                with smtplib.SMTP(mx) as smtpObj:
                    smtpObj.sendmail(oobFrom, oobTo, oobMsg)            # if no exception, the mail is sent (250OK)
                    shareRes.incrementKey('oob_sent')
                    return 'OOB sent,from {} to {} via {}'.format(oobFrom, oobTo, mx)
            except smtplib.SMTPException as err:
                shareRes.incrementKey('oob_smtp_error')
                return '!OOB endpoint returned SMTP error: ' + str(err)


# -----------------------------------------------------------------------------
# Open and Click handling
# -----------------------------------------------------------------------------

# Class holding persistent requests session IDs
class PersistentSession():
    def __init__(self, Nthreads):
        # Set up our connection pool
        self.svec = [None] * Nthreads
        for i in range(0, Nthreads):
            self.svec[i] = requests.session()

    def id(self, i):
        return self.svec[i]

    def size(self):
        return len(self.svec)

# Heuristic for whether this is really SparkPost: it rejects the OPTIONS verb but identifies itself in Server header
def isSparkPostTrackingEndpoint(s, url):
    r = s.options(url, allow_redirects=False, timeout=5)
    return r.status_code == 405 and 'Server' in r.headers and r.headers['Server'] == 'msys-http'

# Improved "GET" - doesn't follow the redirect, and opens as stream (so doesn't actually fetch a lot of stuff)
def touchEndPoint(s, url):
    r = s.get(url, allow_redirects=False, timeout=5, stream=True)

# Parse html email body, looking for open-pixel and links.  Follow these to do open & click tracking
class MyHTMLOpenParser(HTMLParser):
    def __init__(self, s, shareRes):
        HTMLParser.__init__(self)
        self.requestSession = s                             # Use persistent 'requests' session for speed
        self.shareRes = shareRes                            # shared results handle

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            for attrName, attrValue in attrs:
                if attrName == 'src':
                    if isSparkPostTrackingEndpoint(self.requestSession, attrValue):     # attrValue = url
                        touchEndPoint(self.requestSession, attrValue)                   # "open"
                    else:
                        self.shareRes.incrementKey('open_url_not_sparkpost')


class MyHTMLClickParser(HTMLParser):
    def __init__(self, s, shareRes):
        HTMLParser.__init__(self)
        self.requestSession = s                             # Use persistent 'requests' session for speed
        self.shareRes = shareRes                            # shared results handle

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for attrName, attrValue in attrs:
                if attrName == 'href':
                    if isSparkPostTrackingEndpoint(self.requestSession, attrValue):     # attrValue = url
                        touchEndPoint(self.requestSession, attrValue)                   # "click"
                    else:
                        self.shareRes.incrementKey('click_url_not_sparkpost')

# open / open again / click / click again logic, as per conditional probabilities
def openClickMail(mail, probs, shareRes):
    ll = ''
    bd = mail.get_body('text/html')
    if bd:  # if no body to parse, ignore
        body = bd.get_content()                             # this handles quoted-printable type for us
        htmlOpenParser = MyHTMLOpenParser(persist.id(0), shareRes)  # use persistent session for speed
        ll += 'Open'
        shareRes.incrementKey('open')
        htmlOpenParser.feed(body)
        if random.random() <= probs['OpenAgain_Given_Open']:
            htmlOpenParser.feed(body)
            ll += '_OpenAgain'
            shareRes.incrementKey('open_again')
        if random.random() <= probs['Click_Given_Open']:
            htmlClickParser = MyHTMLClickParser(persist.id(0), shareRes)
            htmlClickParser.feed(body)
            ll += '_Click'
            shareRes.incrementKey('click')
            if random.random() <= probs['ClickAgain_Given_Click']:
                htmlClickParser.feed(body)
                ll += '_ClickAgain'
                shareRes.incrementKey('click_again')
    return ll

# -----------------------------------------------------------------------------
# Record results for webReporter
# -----------------------------------------------------------------------------

def checkAndSetFirstRun(st, logger, shareRes):
    k = 'startedRunning'
    res = shareRes.getResult(k)                         # read back results from previous run (if any)
    if not res:
        ok = shareRes.setResult(k, st)
        logger.info('** First run - set {} = {}, ok = {}'.format(k, st, ok))

# -----------------------------------------------------------------------------
# Process a single mail file according to the probabilistic model & special subdomains
# If special subdomains used, these override the model, providing SPF check has passed.
# Actions taken are logged.
# -----------------------------------------------------------------------------

def processMail(mail, fname, probs, logger, shareRes):
    # Log addresses. Some rogue / spammy messages seen are missing From and To addresses
    logline = fname + ',' + xstr(mail['to']) + ',' + xstr(mail['from'])
    shareRes.incrementKey('total_messages')
    # Test that message was checked by PMTA and has valid DKIM signature
    auth = mail['Authentication-Results']
    if auth != None and 'dkim=pass' in auth:
        # Check for special "To" subdomains that signal what action to take (for safety, these also require inbound spf to have passed)
        subd = mail['to'].split('@')[1].split('.')[0]
        if subd == 'oob':
            if 'spf=pass' in auth:
                logline += ',' + oobGen(mail, shareRes)
            else:
                logline += ',!Special ' + subd + ' failed SPF check'
                shareRes.incrementKey('fail_spf')
        elif subd == 'fbl':
            if 'spf=pass' in auth:
                logline += ',' + fblGen(mail, shareRes)
            else:
                logline += ',!Special ' + subd + ' failed SPF check'
                shareRes.incrementKey('fail_spf')
        elif subd == 'openclick':
            logline += ',' + openClickMail(mail, probs, shareRes)       # doesn't need SPF pass
        elif subd == 'accept':
            logline += ',Accept'
            shareRes.incrementKey('accept')
        else:
            # Apply probabilistic model to all other domains
            if random.random() <= probs['OOB']:
                # Mail that out-of-band bounces would not not make it to the inbox, so would not get opened, clicked or FBLd
                logline += ',' + oobGen(mail, shareRes)
            elif random.random() <= probs['FBL']:
                logline += ',' + fblGen(mail, shareRes)
            elif random.random() <= probs['Open']:
                logline += ',' + openClickMail(mail, probs, shareRes)
            else:
                logline += ',Accept'
                shareRes.incrementKey('accept')
    else:
        logline += ',!DKIM fail:' + xstr(auth)
        shareRes.incrementKey('fail_dkim')
    logger.info(logline)


# -----------------------------------------------------------------------------
# Set up probabilistic model for incoming mail
# -----------------------------------------------------------------------------

# Set conditional probability in mutable dict P for event a given event b. https://en.wikipedia.org/wiki/Conditional_probability
def checkSetCondProb(P, a, b, logger):
    aGivenbName = a + '_Given_' + b
    PaGivenb = P[a] / P[b]
    if PaGivenb < 0 or PaGivenb > 1:
        logger.error('Config file problem: {} and {} implies {} = {}, out of range'.format(a, b, aGivenbName, PaGivenb))
        return None
    else:
        P[aGivenbName] = PaGivenb
        return True

# Take the overall percentages and adjust them according to how much traffic we expect to receive. This app would not see
# the 'upstream handled' traffic percentage as PMTA blackholes / in-band-bounces this automatically via PMTA config, not in this application
# Express all values as probabilities 0 <= p <= 1.0
def getBounceProbabilities(cfg, logger):
    try:
        thisAppTraffic  = 1 - cfg.getfloat('Upstream_Handled') / 100
        P = {
            'OOB'       : cfg.getfloat('OOB_percent') / 100 / thisAppTraffic,
            'FBL'       : cfg.getfloat('FBL_percent') / 100 / thisAppTraffic,
            'Open'      : cfg.getfloat('Open_percent') / 100 / thisAppTraffic,
            'OpenAgain' : cfg.getfloat('Open_Again_percent') / 100 / thisAppTraffic,
            'Click'     : cfg.getfloat('Click_percent') / 100 / thisAppTraffic,
            'ClickAgain': cfg.getfloat('Click_Again_percent') / 100 / thisAppTraffic
        }
        # calculate conditional open & click probabilities, given a realistic state sequence would be
        # Open?
        #  - Maybe OpenAgain?
        #  - Maybe Click?
        #     - Maybe ClickAgain?
        if checkSetCondProb(P, 'OpenAgain', 'Open', logger) \
                and checkSetCondProb(P, 'Click', 'Open', logger) \
                and checkSetCondProb(P, 'ClickAgain', 'Click', logger):
            return P
        else:
            return None
    except (ValueError, configparser.Error) as e:
        logger.error('Config file problem: '+str(e))
        return None

# Logging now rotates at midnight (as per the machine's locale)
def consumeFiles(fnameList, cfg):
    startTime = time.time()                                         # measure run time
    # Log some info on mail that is processed
    logfile = cfg.get('Logfile', baseProgName() + '.log')
    logfileBackupCount = cfg.getint('Logfile_backupCount', 10)      # default to 10 files
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fh = logging.handlers.TimedRotatingFileHandler(logfile, when='midnight', backupCount=logfileBackupCount)
    formatter = logging.Formatter('%(asctime)s,%(name)s,%(thread)d,%(levelname)s,%(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    shareRes = Results()                                            # class for sharing summary results

    probs = getBounceProbabilities(cfg, logger)
    if probs:
        checkAndSetFirstRun(timeStr(startTime), logger, shareRes)
        if fnameList is None:
            logger.info('** Consuming mail from stdin')
            msg = email.message_from_file(sys.stdin, policy=policy.default)
            processMail(msg, 'stdin', probs, logger, shareRes)
        else:
            logger.info('** Consuming {} mail file(s)'.format(len(fnameList)))
            countDone = 0
            countSkipped = 0
            for fname in fnameList:
                try:
                    if os.path.isfile(fname):
                        with open(fname) as fIn:
                            os.remove(fname)                        # OK to remove while open, contents destroyed once file handle closed
                            msg = email.message_from_file(fIn, policy=policy.default)
                            processMail(msg, fname, probs, logger, shareRes)
                            countDone += 1
                except Exception as e:                              # catch any exceptions, keep going
                    logger.error(str(e))
                    countSkipped += 1
        endTime = time.time()
        runTime = endTime-startTime
        runRate = (0 if runTime==0 else countDone/runTime)          # Ensure no divide by zero
        logger.info('** Finishing:run time(s)={0:.3f},done {1},skipped {2},done rate={3:.3f}/s'.format(runTime, countDone, countSkipped, runRate) )

# -----------------------------------------------------------------------------
# Main code
# -----------------------------------------------------------------------------
# Take mail input depending on command-line options:
# -f        file        (single file)
# -d        directory   (read process and delete any file with extension .msg)
# (blank)   stdin       (e.g. for pipe input)

config = configparser.ConfigParser()
config.read_file(open(configFileName()))
cfg = config['DEFAULT']
Nthreads = cfg.getint('Threads', 1)
persist = PersistentSession(Nthreads)                               # hold a set of persistent 'requests' sessions

if len(sys.argv) >= 3:
    if sys.argv[1] == '-f':
        consumeFiles([sys.argv[2]], cfg)
    elif sys.argv[1] == '-d':
        dir = sys.argv[2].rstrip('/')                               # strip trailing / if present
        fnameList = glob.glob(os.path.join(dir, '*.msg'))
        consumeFiles(fnameList, cfg)
    else:
        printHelp()
else:
    if len(sys.argv) <= 1:                                          # empty args - read stdin
        consumeFiles(None, cfg)
    else:
        printHelp()