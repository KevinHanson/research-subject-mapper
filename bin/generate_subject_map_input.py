#!/usr/bin/env python
"""

generate_subject_map_input.py -  Tool to generate patient-to-research
subject mapping files based on inputs from REDCap projects

"""
__authors__ = "Mohan Das Katragadda"
__copyright__ = "Copyright 2014, University of Florida"
__license__ = "BSD 3-Clause"
__version__ = "0.1"
__email__ = "mohan88@ufl.edu"
__status__ = "Development"

import xml.etree.ElementTree as ET
import logging
import os
import datetime
from datetime import timedelta
import argparse

from lxml import etree
import appdirs

import gsm_lib
from utils.sftpclient import SFTPClient
from utils.redcap_transactions import redcap_transactions
from utils import SimpleConfigParser

# This addresses the issues with relative paths
file_dir = os.path.dirname(os.path.realpath(__file__))
goal_dir = os.path.join(file_dir, "../")
proj_root = os.path.abspath(goal_dir)+'/'


def main():
    # obtaining command line arguments for path to config directory
    args = parse_args()
    configuration_directory = os.path.abspath(args['configuration_directory_path'])

    # Configure logging
    logger = configure_logging(args['verbose'])

    settings = SimpleConfigParser.SimpleConfigParser()
    settings.read(os.path.join(configuration_directory, 'settings.ini'))
    settings.set_attributes()
    gsm_lib.read_config(configuration_directory, 'settings.ini', settings)

    # Initialize Redcap Interface
    rt = redcap_transactions()
    rt.configuration_directory = configuration_directory

    properties = rt.init_redcap_interface(settings, logger)
    #get data from the redcap for the fields listed in the source_data_schema.xml
    response = rt.get_data_from_redcap(properties, logger)
    logger.debug(response)
    xml_tree = etree.fromstring(response)

    #XSL Transformation 1: This transformation removes junk data, rename elements and extracts site_id and adds new tag site_id
    transform_xsl = os.path.join(configuration_directory, settings.xml_formatting_tranform_xsl)
    xslt = etree.parse(transform_xsl)
    transform = etree.XSLT(xslt)
    xml_transformed = transform(xml_tree)
    xml_str = etree.tostring(xml_transformed, method='xml', pretty_print=True)

    #XSL Transformation 2: This transformation groups the data based on site_id
    transform2_xsl = proj_root + 'bin/utils/groupby_siteid_transform.xsl'
    xslt = etree.parse(transform2_xsl)
    transform = etree.XSLT(xslt)
    xml_transformed2 = transform(xml_transformed)

    #XSL Transformation 3: This transformation removes all the nodes which are not set
    transform3_xsl = proj_root + 'bin/utils/remove_junktags_transform.xsl'
    xslt = etree.parse(transform3_xsl)
    transform = etree.XSLT(xslt)
    xml_transformed3 = transform(xml_transformed2)

    #Prettifying the output generated by XSL Transformation
    xml_str2 = etree.tostring(xml_transformed3, method='xml', pretty_print=True)
    tree = etree.fromstring(xml_str2, etree.XMLParser(remove_blank_text=True))

    # Loop through the start_date elements and update theur values
    for k in tree.iter('start_date'):
        d = datetime.datetime.strptime(k.text, "%Y-%m-%d").date()-timedelta(days=180)
        k.text = str(d)

    #writing data to smi+site_code.xml. This xml will be saved to sftp of the site as smi.xml
    do_keep_gen_files = args['keep']
    tmp_folder = gsm_lib.get_temp_path(do_keep_gen_files)

    subject_map_input = {}
    for k in tree:
        site_code = k.attrib['id']
        file_name = tmp_folder + 'smi' + site_code + '.xml'
        gsm_lib.write_element_tree_to_file(ET.ElementTree(k), file_name)
        subject_map_input[site_code] = file_name

    site_catalog_file = os.path.join(configuration_directory, settings.site_catalog)
    parse_site_details_and_send(site_catalog_file, subject_map_input, logger, settings, do_keep_gen_files)


def parse_args():
    """Parses command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', required=False,
                        dest='configuration_directory_path',
                        default=proj_root + 'config/',
                        help='Specify the path to the configuration directory')

    parser.add_argument('-k', '--keep', required=False,
                        default=False, action='store_true',
                        help='keep files generated during execution')

    parser.add_argument('-v', '--verbose', required=False,
                        default=False, action='store_true',
                        help='increase verbosity of output')

    return vars(parser.parse_args())


def configure_logging(verbose=False):
    """Configures the Logger"""
    application = appdirs.AppDirs(appname='research-subject-mapper', appauthor='University of Florida')

    # create logger for our application
    logger = logging.getLogger(application.appname)
    logger.setLevel(logging.DEBUG)

    # create a console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(relativeCreated)+15s %(name)s - %(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

    # make sure we can write to the log
    gsm_lib.makedirs(application.user_log_dir)
    filename = os.path.join(application.user_log_dir, application.appname + '.log')

    # create a file handler
    file_handler = None
    try:
        file_handler = logging.FileHandler(filename)
    except IOError:
        logger.exception('Could not open file for logging "%s"', filename)

    if file_handler:
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
        logger.debug('Log file will be "%s"', filename)
        logger.addHandler(file_handler)
    else:
        logger.warning('File logging has been disabled.')

    return logger


def parse_site_details_and_send(site_catalog_file, subject_map_input, logger, settings, keep_files):
    """
    Parses the site details from site catalog

    Note: uses files generated by `write_element_tree_to_file` in the main
    function.
    """
    try:
        site_data = etree.parse(site_catalog_file)
    except IOError:
        logger.exception("Could not open Site Catalog file: '%s'. "
                         "Check 'site_catalog' in your settings file.",
                         site_catalog_file)
        raise

    logger.info("Processing site details and uploading subject map input files")
    site_num = len(site_data.findall(".//site"))
    logger.debug("%s total subject site entries read into tree.", site_num)

    for site in site_data.iter('site'):
        site_code = site.findtext('site_code')
        logger.debug("Processing site '%s'.", site_code)
        if site_code not in subject_map_input.keys():
            logger.warning('Site code "%s" defined in Site Catalog, but no '
                           'records found for that site.', site_code)
            continue

        host, port = gsm_lib.parse_host_and_port(site.findtext('site_URI'))
        logger.debug("Server: %s:%s", host, port)
        site_uname = site.findtext('site_uname')
        logger.debug("Username: %s", site_uname)
        site_key_path = site.findtext('site_key_path')
        site_password = site.findtext('site_password')
        site_contact_email = site.findtext('site_contact_email')
        logger.debug("Site Contact Email: %s", site_contact_email)
        sender_email = settings.sender_email
        logger.debug("Sender Email: %s", sender_email)

        # Pick up the correct smi file with the code and place in the
        # destination as smi.xml at the specified remote path
        site_remotepath = site.findtext('site_remotepath')
        site_localpath = subject_map_input[site_code]

        logger.info('Sending %s to %s: %s', site_localpath, host, site_remotepath)
        logger.debug('Any errors will be emailed to '+site_contact_email)

        sftp_instance = SFTPClient(host, sender_email, port, site_uname, site_password,
                                   private_key=site_key_path)
        sftp_instance.send_file_to_uri(site_remotepath, 'smi.xml',
                                       site_localpath, site_contact_email)

        if keep_files:
            logger.debug('Keeping the temporary file: ' + site_localpath)
        else:
            # remove the smi.xml from the folder
            logger.debug('Removing the temporary file: ' + site_localpath)
            try:
                os.remove(site_localpath)
            except OSError as error:
                logger.warning(error)


if __name__ == "__main__":
    main()
