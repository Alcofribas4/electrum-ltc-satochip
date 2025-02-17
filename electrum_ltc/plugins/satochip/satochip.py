from struct import pack, unpack
from os import urandom
import hashlib
import hmac
import sys
import traceback

#electrum
from electrum_ltc import mnemonic
#from electrum_ltc import bitcoin
from electrum_ltc import constants
from electrum_ltc.bitcoin import TYPE_ADDRESS, int_to_hex, var_int
from electrum_ltc.i18n import _
from electrum_ltc.plugin import BasePlugin, Device
from electrum_ltc.keystore import Hardware_KeyStore
from electrum_ltc.transaction import Transaction
from electrum_ltc.wallet import Standard_Wallet
from electrum_ltc.util import bfh, bh2u, versiontuple
from electrum_ltc.base_wizard import ScriptTypeNotSupported
from electrum_ltc.crypto import hash_160, sha256d
from electrum_ltc.ecc import CURVE_ORDER, der_sig_from_r_and_s, get_r_and_s_from_der_sig, ECPubkey
from electrum_ltc.mnemonic import Mnemonic
from electrum_ltc.keystore import bip39_to_seed
from electrum_ltc.plugin import run_hook
from electrum_ltc.bip32 import BIP32Node
from electrum_ltc.logging import get_logger

from electrum_ltc.gui.qt.qrcodewidget import QRCodeWidget, QRDialog

from ..hw_wallet import HW_PluginBase
from ..hw_wallet.plugin import is_any_tx_output_on_change_branch

#pysatochip
from .CardConnector import CardConnector, UninitializedSeedError
from .CardDataParser import CardDataParser
from .JCconstants import JCconstants
from .TxParser import TxParser

from smartcard.sw.SWExceptions import SWException
from smartcard.Exceptions import CardConnectionException, CardRequestTimeoutException
from smartcard.CardType import AnyCardType
from smartcard.CardRequest import CardRequest

_logger = get_logger(__name__)

# debug: smartcard reader ids
SATOCHIP_VID= 0 #0x096E
SATOCHIP_PID= 0 #0x0503

MSG_USE_2FA= _("Do you want to use 2-Factor-Authentication (2FA)?\n\nWith 2FA, any transaction must be confirmed on a second device such as your smartphone. First you have to install the Satochip-2FA android app on google play. Then you have to pair your 2FA device with your Satochip by scanning the qr-code on the next screen. Warning: be sure to backup a copy of the qr-code in a safe place, in case you have to reinstall the app!")

def bip32path2bytes(bip32path:str) -> (int, bytes):
    splitPath = bip32path.split('/')
    splitPath=[x for x in splitPath if x] # removes empty values
    if splitPath[0] == 'm':
        splitPath = splitPath[1:]
        #bip32path = bip32path[2:]
    
    bytePath=b''
    depth= len(splitPath)    
    for index in splitPath:
        if index.endswith("'"):
           bytePath+= pack( ">I", int(index.rstrip("'"))+0x80000000 )   
        else:
           bytePath+=pack( ">I", int(index) )
        
    return (depth, bytePath)

class SatochipClient():
    def __init__(self, plugin, handler):
        _logger.info(f"[SatochipClient] __init__()")#debugSatochip
        _logger.info(f"Type of handler: {type(handler)}") #debugSatochip
        self.device = plugin.device
        self.handler = handler
        self.parser= CardDataParser()
        self.cc= CardConnector(self)
        
        # debug 
        try:
            _logger.info(f"[SatochipClient] __init__(): ATR:{self.cc.card_get_ATR()}")#debugSatochip
            (response, sw1, sw2)=self.cc.card_select()
        except SWException as e:
            _logger.exception(f"Exception during SatochipClient initialization: {str(e)}")
            
    def __repr__(self):
        return '<SatochipClient TODO>'
        
    def is_pairable(self):
        return True

    def close(self):
        _logger.info(f"[SatochipClient] close()")#debugSatochip
        self.cc.card_disconnect()
        self.cc.cardmonitor.deleteObserver(self.cc.cardobserver)
        
    def timeout(self, cutoff):
        pass

    def is_initialized(self):
        _logger.info(f"[SatochipClient] is_initialized(): TODO - currently set to true!")#debugSatochip
        return True

    def label(self):
        _logger.info(f"[SatochipClient] label(): TODO - currently empty")#debugSatochip
        return ""

    def i4b(self, x):
        return pack('>I', x)

    def has_usable_connection_with_device(self):
        try:
            (response, sw1, sw2)=self.cc.card_select() #TODO: something else?
        except SWException as e:
            _logger.exception(f"Exception: {str(e)}")
            return False
        return True

    def get_xpub(self, bip32_path, xtype):
        assert xtype in SatochipPlugin.SUPPORTED_XTYPES
        
        # try:
            # hex_authentikey= self.handler.win.wallet.storage.get('authentikey')
            # _logger.info(f"[SatochipClient] get_xpub(): self.handler.win.wallet.storage.authentikey:{str(hex_authentikey)}")#debugSatochip
            # if hex_authentikey is not None:
                # self.parser.authentikey_from_storage= ECPubkey(bytes.fromhex(hex_authentikey))
        # except Exception as e: #attributeError?
            # _logger.exception(f"Exception when getting authentikey from self.handler.win.wallet.storage:{str(e)}")#debugSatochip
        
        # bip32_path is of the form 44'/0'/1'
        _logger.info(f"[SatochipClient] get_xpub(): bip32_path={bip32_path}")#debugSatochip
        (depth, bytepath)= bip32path2bytes(bip32_path)
        (childkey, childchaincode)= self.cc.card_bip32_get_extendedkey(bytepath)
        if depth == 0: #masterkey
            fingerprint= bytes([0,0,0,0])
            child_number= bytes([0,0,0,0])
        else: #get parent info
            (parentkey, parentchaincode)= self.cc.card_bip32_get_extendedkey(bytepath[0:-4])
            fingerprint= hash_160(parentkey.get_public_key_bytes(compressed=True))[0:4]
            child_number= bytepath[-4:]
        #xpub= serialize_xpub(xtype, childchaincode, childkey.get_public_key_bytes(compressed=True), depth, fingerprint, child_number)
        xpub= BIP32Node(xtype=xtype,
                         eckey=childkey,
                         chaincode=childchaincode,
                         depth=depth,
                         fingerprint=fingerprint,
                         child_number=child_number).to_xpub()
        _logger.info(f"[SatochipClient] get_xpub(): xpub={str(xpub)}")#debugSatochip
        return xpub        
        
    def ping_check(self):
        #check connection is working
        try: 
            atr= self.cc.card_get_ATR()
        except Exception as e:
            _logger.exception(f"Exception: {str(e)}")
            raise RuntimeError("Communication issue with Satochip")
        
    def perform_hw1_preflight(self):
        pass

    def checkDevice(self):
        if not self.preflightDone:
            try:
                self.perform_hw1_preflight()
            except Exception as e:
                print(e)
            self.preflightDone = True

    def PIN_dialog(self, msg):
        while True:
            password = self.handler.get_passphrase(msg, False)
            if password is None:
                return False, None, None
            if len(password) < 4:
                msg = _("PIN must have at least 4 characters.") + \
                      "\n\n" + _("Enter PIN:")
            elif len(password) > 64:
                msg = _("PIN must have less than 64 characters.") + \
                      "\n\n" + _("Enter PIN:")
            else:
                self.PIN = password.encode('utf8')
                return True, self.PIN, self.PIN    

                
class Satochip_KeyStore(Hardware_KeyStore):       
    hw_type = 'satochip'
    device = 'Satochip'
    
    def __init__(self, d):
        Hardware_KeyStore.__init__(self, d)
        #_logger.info(f"[Satochip_KeyStore] __init__(): xpub:{str(d.get('xpub'))}")#debugSatochip
        #_logger.info(f"[Satochip_KeyStore] __init__(): derivation:{str(d.get('derivation'))}")#debugSatochip
        self.force_watching_only = False
        self.ux_busy = False
         
    def dump(self):
        # our additions to the stored data about keystore -- only during creation?
        d = Hardware_KeyStore.dump(self)
        return d

    def get_derivation(self):
        return self.derivation

    def get_client(self):
        # called when user tries to do something like view address, sign something.
        # - not called during probing/setup
        rv = self.plugin.get_client(self)
        return rv
        
    def give_error(self, message, clear_client=False):
        _logger.info(message)
        if not self.ux_busy:
            self.handler.show_error(message)
        else:
            self.ux_busy = False
        if clear_client:
            self.client = None
        raise Exception(message)
    
    def decrypt_message(self, pubkey, message, password):
        raise RuntimeError(_('Encryption and decryption are currently not supported for {}').format(self.device))
        
    def sign_message(self, sequence, message, password):
        message = message.encode('utf8')
        message_hash = hashlib.sha256(message).hexdigest().upper()
        client = self.get_client()
        address_path = self.get_derivation()[2:] + "/%d/%d"%sequence
        _logger.info(f"[Satochip_KeyStore] sign_message: path: {address_path}")
        self.handler.show_message("Signing message ...\r\nMessage hash: "+message_hash)
        try:
            #path= self.get_derivation() + ("/%d/%d" % sequence)
            keynbr= 0xFF #for extended key
            (depth, bytepath)= bip32path2bytes(address_path)
            (key, chaincode)=client.cc.card_bip32_get_extendedkey(bytepath)
            (response2, sw1, sw2) = client.cc.card_sign_message(keynbr, message)
            compsig=client.parser.parse_message_signature(response2, message, key)
            
        except Exception as e:
            self.give_error(e, True)
        finally:
            self.handler.finished()
        return compsig
        
    def sign_transaction(self, tx, password):
        _logger.info(f"[Satochip_KeyStore] sign_transaction(): tx: {str(tx)}") #debugSatochip
        client = self.get_client()
        segwitTransaction = False
        
        # outputs
        txOutputs= ''.join(tx.serialize_output(o) for o in tx.outputs())
        hashOutputs = bh2u(sha256d(bfh(txOutputs)))
        txOutputs = var_int(len(tx.outputs()))+txOutputs
        _logger.info(f"[Satochip_KeyStore] sign_transaction(): hashOutputs= {hashOutputs}") #debugSatochip
        _logger.info(f"[Satochip_KeyStore] sign_transaction(): outputs= {txOutputs}") #debugSatochip
        
        # Fetch inputs of the transaction to sign
        derivations = self.get_tx_derivations(tx)
        for i,txin in enumerate(tx.inputs()):
            _logger.info(f"[Satochip_KeyStore] sign_transaction(): input= {str(i)} - input[type]: {txin['type']}") #debugSatochip
            if txin['type'] == 'coinbase':
                self.give_error("Coinbase not supported")     # should never happen

            if txin['type'] in ['p2sh']:
                p2shTransaction = True

            if txin['type'] in ['p2wpkh-p2sh', 'p2wsh-p2sh']:
                segwitTransaction = True

            if txin['type'] in ['p2wpkh', 'p2wsh']:
                segwitTransaction = True
            
            pubkeys, x_pubkeys = tx.get_sorted_pubkeys(txin)
            for j, x_pubkey in enumerate(x_pubkeys):
                _logger.info(f"[Satochip_KeyStore] sign_transaction(): forforloop: j= {str(j)}") #debugSatochip
                if tx.is_txin_complete(txin):
                    break
                    
                if x_pubkey in derivations:
                    signingPos = j
                    s = derivations.get(x_pubkey)
                    address_path = "%s/%d/%d" % (self.get_derivation()[2:], s[0], s[1])
                    
                    # get corresponing extended key
                    (depth, bytepath)= bip32path2bytes(address_path)
                    (key, chaincode)=client.cc.card_bip32_get_extendedkey(bytepath)
                    
                    # parse tx
                    pre_tx_hex= tx.serialize_preimage(i)
                    pre_tx= bytes.fromhex(pre_tx_hex)# hex representation => converted to bytes
                    pre_hash = sha256d(bfh(pre_tx_hex))
                    pre_hash_hex= pre_hash.hex()
                    _logger.info(f"[Satochip_KeyStore] sign_transaction(): pre_tx_hex= {pre_tx_hex}") #debugSatochip
                    _logger.info(f"[Satochip_KeyStore] sign_transaction(): pre_hash= {pre_hash_hex}") #debugSatochip
                    (response, sw1, sw2) = client.cc.card_parse_transaction(pre_tx, segwitTransaction)
                    (tx_hash, needs_2fa)= client.parser.parse_parse_transaction(response)
                    tx_hash_hex= bytearray(tx_hash).hex()
                    if pre_hash_hex!= tx_hash_hex:
                        raise RuntimeError("[Satochip_KeyStore] Tx preimage mismatch: {pre_hash_hex} vs {tx_hash_hex}")
                    
                    # sign tx
                    keynbr= 0xFF #for extended key
                    if needs_2fa:
                        # format & encrypt msg
                        import json
                        coin_type= 2 #see https://github.com/satoshilabs/slips/blob/master/slip-0044.md
                        test_net= constants.net.TESTNET
                        if segwitTransaction:
                            msg= {'tx':pre_tx_hex, 'ct':coin_type, 'tn':test_net, 'sw':segwitTransaction, 'txo':txOutputs, 'ty':txin['type']} 
                        else:
                            msg= {'tx':pre_tx_hex, 'ct':coin_type, 'tn':test_net, 'sw':segwitTransaction} 
                        msg=  json.dumps(msg)
                        (id_2FA, msg_out)= client.cc.card_crypt_transaction_2FA(msg, True)
                        d={}
                        d['msg_encrypt']= msg_out
                        d['id_2FA']= id_2FA
                        # _logger.info(f"encrypted message: {msg_out}")
                        _logger.info(f"id_2FA: {id_2FA}")
                        
                        #do challenge-response with 2FA device...
                        client.handler.show_message('2FA request sent! Approve or reject request on your second device.')
                        run_hook('do_challenge_response', d)
                        # decrypt and parse reply to extract challenge response
                        try: 
                            reply_encrypt= d['reply_encrypt']
                        except Exception as e:
                            self.give_error("No response received from 2FA.\nPlease ensure that the Satochip-2FA plugin is enabled in Tools>Optional Features", True)
                        if reply_encrypt is None:
                            #todo: abort tx
                            break
                        reply_decrypt= client.cc.card_crypt_transaction_2FA(reply_encrypt, False)
                        _logger.info(f"[Satochip_KeyStore] sign_transaction(): challenge:response= {reply_decrypt}")
                        reply_decrypt= reply_decrypt.split(":")
                        rep_pre_hash_hex= reply_decrypt[0]
                        if rep_pre_hash_hex!= pre_hash_hex:
                            #todo: abort tx or retry?
                            break
                        chalresponse=reply_decrypt[1]
                        if chalresponse=="00"*20:
                            #todo: abort tx?
                            break
                        chalresponse= list(bytes.fromhex(chalresponse))
                    else:
                        chalresponse= None
                    (tx_sig, sw1, sw2) = client.cc.card_sign_transaction(keynbr, tx_hash, chalresponse)
                    #_logger.info(f"sign_transaction(): sig= {bytearray(tx_sig).hex()}") #debugSatochip
                    #todo: check sw1sw2 for error (0x9c0b if wrong challenge-response)
                    # enforce low-S signature (BIP 62)
                    tx_sig = bytearray(tx_sig)
                    r,s= get_r_and_s_from_der_sig(tx_sig)
                    if s > CURVE_ORDER//2:
                        s = CURVE_ORDER - s
                    tx_sig=der_sig_from_r_and_s(r, s)
                    #update tx with signature
                    tx_sig = tx_sig.hex()+'01'
                    tx.add_signature_to_txin(i,j,tx_sig)
                    break
            else:
                self.give_error("No matching x_key for sign_transaction") # should never happen
            
        _logger.info(f"[Satochip_KeyStore] sign_transaction(): Tx is complete: {str(tx.is_complete())}")
        tx.raw = tx.serialize()    
        return
    
    def show_address(self, sequence, txin_type):
        _logger.info(f'[Satochip_KeyStore] show_address(): todo!')
        return
    
        
class SatochipPlugin(HW_PluginBase):        
    libraries_available= True
    minimum_library = (0, 0, 0)
    keystore_class= Satochip_KeyStore
    DEVICE_IDS= [
       (SATOCHIP_VID, SATOCHIP_PID) 
    ]
    SUPPORTED_XTYPES = ('standard', 'p2wpkh-p2sh', 'p2wpkh', 'p2wsh-p2sh', 'p2wsh')
       
    def __init__(self, parent, config, name):
        
        _logger.info(f"[SatochipPlugin] init()")#debugSatochip
        HW_PluginBase.__init__(self, parent, config, name)

        #self.libraries_available = self.check_libraries_available() #debugSatochip
        #if not self.libraries_available:
        #    return

        #self.device_manager().register_devices(self.DEVICE_IDS)
        self.device_manager().register_enumerate_func(self.detect_smartcard_reader)
        
    def get_library_version(self):
        return '0.0.1'
    
    def detect_smartcard_reader(self):
        _logger.info(f"[SatochipPlugin] detect_smartcard_reader")#debugSatochip
        self.cardtype = AnyCardType()
        try:
            cardrequest = CardRequest(timeout=5, cardType=self.cardtype)
            cardservice = cardrequest.waitforcard()
            return [Device(path="/satochip",
                           interface_number=-1,
                           id_="/satochip",
                           product_key=(SATOCHIP_VID,SATOCHIP_PID),
                           usage_page=0,
                           transport_ui_string='ccid')]
        except CardRequestTimeoutException:
            _logger.info(f'time-out: no card inserted during last 5s')
            return []
        except Exception as exc:
            _logger.info(f"Error during connection:{str(exc)}")
            return []
        return []
        
    
    def create_client(self, device, handler):
        _logger.info(f"[SatochipPlugin] create_client()")#debugSatochip
        
        if handler:
            self.handler = handler

        try:
            rv = SatochipClient(self, handler)
            return rv
        except Exception as e:
            _logger.exception(f"[SatochipPlugin] create_client() exception: {str(e)}")
            return None

    def setup_device(self, device_info, wizard, purpose):
        _logger.info(f"[SatochipPlugin] setup_device()")#debugSatochip
        
        devmgr = self.device_manager()
        device_id = device_info.device.id_
        client = devmgr.client_by_id(device_id)
        if client is None:
            raise Exception(_('Failed to create a client for this device.') + '\n' +
                            _('Make sure it is in the correct state.'))
        client.handler = self.create_handler(wizard)
        
        # check applet version
        while(True):
            (response, sw1, sw2, d)=client.cc.card_get_status()
            if (sw1==0x90 and sw2==0x00):
                v_supported= (CardConnector.SATOCHIP_PROTOCOL_MAJOR_VERSION<<8)+CardConnector.SATOCHIP_PROTOCOL_MINOR_VERSION
                v_applet= (d["protocol_major_version"]<<8)+d["protocol_minor_version"] 
                strcmp= 'lower' if (v_applet<v_supported) else 'higher'   
                _logger.info(f"[SatochipPlugin] setup_device(): Satochip version={hex(v_applet)} Electrum supported version= {hex(v_supported)}")#debugSatochip
                if (v_supported!=v_applet):
                    msg=_('The version of your Satochip (v{v_applet_maj:x}.{v_applet_min:x}) is {strcmp} than supported by Electrum (v{v_supported_maj:x}.{v_supported_min:x}). You should update Electrum to ensure correct function!').format(strcmp=strcmp, v_applet_maj=d["protocol_major_version"], v_applet_min=d["protocol_minor_version"],  v_supported_maj=CardConnector.SATOCHIP_PROTOCOL_MAJOR_VERSION, v_supported_min=CardConnector.SATOCHIP_PROTOCOL_MINOR_VERSION)
                    client.handler.show_error(msg)
                break
            # setup device (done only once)
            elif (sw1==0x9c and sw2==0x04):
                # PIN dialog
                while (True):
                    msg = _("Enter a new PIN for your Satochip:")
                    (is_PIN, pin_0, pin_0)= client.PIN_dialog(msg)
                    msg = _("Please confirm the PIN code for your Satochip:")
                    (is_PIN, pin_confirm, pin_confirm)= client.PIN_dialog(msg)
                    if (pin_0 != pin_confirm):
                        msg= _("The PIN values do not match! Please type PIN again!")
                        client.handler.show_error(msg)
                    else:
                        break
                pin_0= list(pin_0)
                client.cc.set_pin(0, pin_0) #cache PIN value in client
                pin_tries_0= 0x05;
                ublk_tries_0= 0x01;
                # PUK code can be used when PIN is unknown and the card is locked
                # We use a random value as the PUK is not used currently in the electrum GUI
                ublk_0= list(urandom(16)); 
                pin_tries_1= 0x01
                ublk_tries_1= 0x01
                pin_1= list(urandom(16)); #the second pin is not used currently
                ublk_1= list(urandom(16));
                secmemsize= 32 # number of slot reserved in memory cache
                memsize= 0x0000 # RFU
                create_object_ACL= 0x01 # RFU
                create_key_ACL= 0x01 # RFU
                create_pin_ACL= 0x01 # RFU
                
                # Optionnaly setup 2-Factor-Authentication (2FA)
                #msg= "Do you want to use 2-Factor-Authentication?"
                use_2FA=client.handler.yes_no_question(MSG_USE_2FA)
                _logger.info(f"[SatochipPlugin] setup_device(): perform cardSetup:")#debugSatochip
                if (use_2FA):
                    option_flags= 0x8000 # activate 2fa with hmac challenge-response
                    secret_2FA= urandom(20)
                    secret_2FA_hex=secret_2FA.hex()
                    amount_limit= 0 # always use 
                    (response, sw1, sw2)=client.cc.card_setup(pin_tries_0, ublk_tries_0, pin_0, ublk_0,
                        pin_tries_1, ublk_tries_1, pin_1, ublk_1, 
                        secmemsize, memsize, 
                        create_object_ACL, create_key_ACL, create_pin_ACL,
                        option_flags, list(secret_2FA), amount_limit)
                    # the secret must be shared with the second factor app (eg on a smartphone)
                    try:
                        d = QRDialog(secret_2FA_hex, None, "Secret_2FA", True)
                        d.exec_()
                    except Exception as e:
                        _logger.exception(f"[SatochipPlugin] setup_device(): setup 2FA: {str(e)}")
                    # further communications will require an id and an encryption key (for privacy). 
                    # Both are derived from the secret_2FA using a one-way function inside the Satochip
                else:
                    (response, sw1, sw2)=client.cc.card_setup(pin_tries_0, ublk_tries_0, pin_0, ublk_0,
                        pin_tries_1, ublk_tries_1, pin_1, ublk_1, 
                        secmemsize, memsize, 
                        create_object_ACL, create_key_ACL, create_pin_ACL)
                if sw1!=0x90 or sw2!=0x00:                 
                    _logger.info(f"[SatochipPlugin] setup_device(): unable to set up applet!  sw12={hex(sw1)} {hex(sw2)}")#debugSatochip
                    raise RuntimeError('Unable to setup the device with error code:'+hex(sw1)+' '+hex(sw2))
            else:
                _logger.info(f"[SatochipPlugin] unknown get-status() error! sw12={hex(sw1)} {hex(sw2)}")#debugSatochip
                raise RuntimeError('Unknown get-status() error code:'+hex(sw1)+' '+hex(sw2))
            
        # verify pin:
        client.cc.card_verify_PIN()
                
        # get authentikey
        while(True):
            try:
                authentikey=client.cc.card_bip32_get_authentikey()
            except UninitializedSeedError:
                # test seed dialog...
                _logger.info(f"[SatochipPlugin] setup_device(): import seed") #debugSatochip
                self.choose_seed(wizard)
                seed= list(self.bip32_seed)
                #seed= bytes([0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]) # Bip32 test vectors
                authentikey= client.cc.card_bip32_import_seed(seed)
            hex_authentikey= authentikey.get_public_key_hex(compressed=True)
            _logger.info(f"[SatochipPlugin] setup_device(): authentikey={hex_authentikey}")#debugSatochip
            #wizard.storage.put('authentikey', hex_authentikey)
            wizard.data['authentikey']= hex_authentikey
            client.parser.authentikey_from_storage= authentikey
            break
        
    def get_xpub(self, device_id, derivation, xtype, wizard):
        # this seems to be part of the pairing process only, not during normal ops?
        # base_wizard:on_hw_derivation
        _logger.info(f"[SatochipPlugin] get_xpub()")#debugSatochip
        if xtype not in self.SUPPORTED_XTYPES:
            raise ScriptTypeNotSupported(_('This type of script is not supported with {}.').format(self.device))
        devmgr = self.device_manager()
        client = devmgr.client_by_id(device_id)
        client.handler = self.create_handler(wizard)
        client.ping_check()
           
        xpub = client.get_xpub(derivation, xtype)
        return xpub
    
    def get_client(self, keystore, force_pair=True):
        # All client interaction should not be in the main GUI thread
        devmgr = self.device_manager()
        handler = keystore.handler
        with devmgr.hid_lock:
            client = devmgr.client_for_keystore(self, handler, keystore, force_pair)
        # returns the client for a given keystore. can use xpub
        #if client:
        #    client.used()
        if client is not None:
            client.ping_check()
        return client
    
    def show_address(self, wallet, address, keystore=None):
        if keystore is None:
            keystore = wallet.get_keystore()
        if not self.show_address_helper(wallet, address, keystore):
            return

        # Standard_Wallet => not multisig, must be bip32
        if type(wallet) is not Standard_Wallet:
            keystore.handler.show_error(_('This function is only available for standard wallets when using {}.').format(self.device))
            return

        sequence = wallet.get_address_index(address)
        txin_type = wallet.get_txin_type(address)
        keystore.show_address(sequence, txin_type)
    
    # create/restore seed during satochip initialization
    def choose_seed(self, wizard):
        title = _('Create or restore')
        message = _('Do you want to create a new seed, or to restore a wallet using an existing seed?')
        choices = [
            ('create_seed', _('Create a new seed')),
            ('restore_from_seed', _('I already have a seed')),
        ]
        wizard.choice_dialog(title=title, message=message, choices=choices, run_next=wizard.run)
    #create seed
    def create_seed(self, wizard):
        wizard.seed_type = 'standard'
        wizard.opt_bip39 = False
        seed = Mnemonic('en').make_seed(wizard.seed_type)
        f = lambda x: self.request_passphrase(wizard, seed, x)
        wizard.show_seed_dialog(run_next=f, seed_text=seed)

    def request_passphrase(self, wizard, seed, opt_passphrase):
        if opt_passphrase:
            f = lambda x: self.confirm_seed(wizard, seed, x)
            wizard.passphrase_dialog(run_next=f)
        else:
            wizard.run('confirm_seed', seed, '')

    def confirm_seed(self, wizard, seed, passphrase):
        f = lambda x: self.confirm_passphrase(wizard, seed, passphrase)
        wizard.confirm_seed_dialog(run_next=f, test=lambda x: x==seed)

    def confirm_passphrase(self, wizard, seed, passphrase):
        f = lambda x: self.derive_bip32_seed(seed, x)
        if passphrase:
            title = _('Confirm Seed Extension')
            message = '\n'.join([
                _('Your seed extension must be saved together with your seed.'),
                _('Please type it here.'),
            ])
            wizard.line_dialog(run_next=f, title=title, message=message, default='', test=lambda x: x==passphrase)
        else:
            f('')    
    
    def derive_bip32_seed(self, seed, passphrase):
        self.bip32_seed= Mnemonic('en').mnemonic_to_seed(seed, passphrase)
    
    #restore from seed
    def restore_from_seed(self, wizard):
        wizard.opt_bip39 = True
        wizard.opt_ext = True
        #is_cosigning_seed = lambda x: seed_type(x) in ['standard', 'segwit']
        test = mnemonic.is_seed #if self.wallet_type == 'standard' else is_cosigning_seed
        f= lambda seed, is_bip39, is_ext: self.on_restore_seed(wizard, seed, is_bip39, is_ext)
        wizard.restore_seed_dialog(run_next=f, test=test)
        
    def on_restore_seed(self, wizard, seed, is_bip39, is_ext):
        wizard.seed_type = 'bip39' if is_bip39 else mnemonic.seed_type(seed)
        if wizard.seed_type == 'bip39':
            f = lambda passphrase: self.derive_bip39_seed(seed, passphrase)
            wizard.passphrase_dialog(run_next=f, is_restoring=True) if is_ext else f('')
        elif wizard.seed_type in ['standard', 'segwit']:
            f = lambda passphrase: self.derive_bip32_seed(seed, passphrase)
            wizard.passphrase_dialog(run_next=f, is_restoring=True) if is_ext else f('')
        elif wizard.seed_type == 'old':
            raise Exception('Unsupported seed type', wizard.seed_type)
        elif mnemonic.is_any_2fa_seed_type(wizard.seed_type):
            raise Exception('Unsupported seed type', wizard.seed_type)
        else:
            raise Exception('Unknown seed type', wizard.seed_type)

    def derive_bip39_seed(self, seed, passphrase):
        self.bip32_seed=bip39_to_seed(seed, passphrase)
        
    

    
        
        
    
    
    
    