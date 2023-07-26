import logging
import oci
import re
import time
from oci.core import ComputeClient, VirtualNetworkClient
from oci.config import validate_config
import sys
import requests
import random
import base64
import yaml


def telegram(desp):
    data = (('chat_id', config['telegram']['user_id']), ('text', '🐢甲骨文ARM抢注脚本为您播报🐢 \n\n' + desp))
    response = requests.post('https://' + config['telegram']['api_host'] + '/bot' + config['telegram']['bot_token'] +
                             '/sendMessage', data=data)
    if response.status_code != 200:
        logging.error(f'Telegram Bot 推送失败, {response.text}')
    else:
        logger.info('Telegram Bot 推送成功')


class OciUser:
    """
    oci 用户配置文件的类
    """
    user: str
    fingerprint: str
    key_file: str
    tenancy: str
    region: str

    def __init__(self, configfile="~/.oci/config", profile="DEFAULT"):
        # todo 用户可以自定义制定config文件地址，暂时懒得写
        cfg = oci.config.from_file(file_location=configfile,
                                   profile_name=profile)
        validate_config(cfg)
        self.parse(cfg)

    def parse(self, cfg) -> None:
        logger.debug("parser cfg")
        self.user = cfg['user']
        self.fingerprint = cfg["fingerprint"]
        self.key_file = cfg["key_file"]
        self.tenancy = cfg['tenancy']
        self.region = cfg['region']

    def keys(self):
        return "user", "fingerprint", "key_file", "tenancy", "region"

    def __getitem__(self, item):
        return getattr(self, item)

    def compartment_id(self):
        return self.tenancy


class FileParser:
    def __init__(self, file_path: str) -> None:
        self.parser(file_path)

    def parser(self, file_path):
        # compoartment id
        logger.debug("开始解析参数")

        try:
            logger.debug(f"filepath {file_path}")
            f = open(file_path, "r")
            self._filebuf = f.read()
            f.close()

        except Exception as e:
            logging.error(f"main.tf文件打开失败,请再一次确认执行了正确操作,脚本退出, {e}")
            exit(0)

        compoartment_pat = re.compile('compartment_id = "(.*)"')
        self.compoartment_id = compoartment_pat.findall(self._filebuf).pop()

        # 内存
        memory_pat = re.compile('memory_in_gbs = "(.*)"')
        self.memory_in_gbs = float(memory_pat.findall(self._filebuf).pop())
        # 查找cpu个数
        cpu_pat = re.compile('ocpus = "(.*)"')
        self.ocpus = float(cpu_pat.findall(self._filebuf).pop())

        # 可用域
        ava_domain_pat = re.compile('availability_domain = "(.*)"')

        self.availability_domain = ava_domain_pat.findall(self._filebuf).pop()

        # 子网id
        subnet_pat = re.compile('subnet_id = "(.*)"')
        self.subnet_id = subnet_pat.findall(self._filebuf).pop()
        # 实例名称
        disname_pat = re.compile('display_name = "(.*)"')
        disname = disname_pat.findall(self._filebuf).pop()
        self.display_name = disname.strip().replace(" ", "-")

        # imageid
        imageid_pat = re.compile('source_id = "(.*)"')
        self.image_id = imageid_pat.findall(self._filebuf)[0]
        # 硬盘大小
        oot_volume_size_in_gbs_pat = re.compile(
            'boot_volume_size_in_gbs = "(.*)"')
        try:
            self.boot_volume_size_in_gbs = float(
                oot_volume_size_in_gbs_pat.findall(self._filebuf).pop())
        except IndexError:
            self.boot_volume_size_in_gbs = 50.0

        logger.debug(f"硬盘大小, {self.boot_volume_size_in_gbs}GB")
        # 读取密钥
        ssh_rsa_pat = re.compile('"ssh_authorized_keys" = "(.*)"')
        try:
            self.ssh_authorized_keys = ssh_rsa_pat.findall(self._filebuf).pop()
        except Exception as e:
            logging.warning("推荐创建堆栈的时候下载ssh key，理论上是可以不用的，但是我没写😂,麻烦重新创建吧")

    @property
    def ssh_authorized_keys(self):
        self._sshkey

    @ssh_authorized_keys.setter
    def ssh_authorized_keys(self, key):
        self._sshkey = key

    @property
    def boot_volume_size_in_gbs(self):
        return self._volsize

    @boot_volume_size_in_gbs.setter
    def boot_volume_size_in_gbs(self, size):
        self._volsize = size

    @property
    def image_id(self):
        return self._imgid

    @image_id.setter
    def image_id(self, imageid):
        self._imgid = imageid

    @property
    def display_name(self):
        return self._dname

    @display_name.setter
    def display_name(self, name):
        self._dname = name

    @property
    def subnet_id(self):
        return self._subid

    @subnet_id.setter
    def subnet_id(self, sid):
        self._subid = sid

    @property
    def compoartment_id(self):
        return self._comid

    @compoartment_id.setter
    def compoartment_id(self, cid):
        self._comid = cid

    @property
    def memory_in_gbs(self):
        return self._mm

    @memory_in_gbs.setter
    def memory_in_gbs(self, mm):
        self._mm = mm

    @property
    def ocpus(self):
        return self._cpu

    @ocpus.setter
    def ocpus(self, cpu_count):
        self._cpu = cpu_count

    @property
    def availability_domain(self):
        return self._adomain

    @availability_domain.setter
    def availability_domain(self, domain):
        self._adomain = domain


class InsCreate:
    shape = 'VM.Standard.A1.Flex'
    sleep_time = 5.0
    try_count = 0
    desp = ""

    def __init__(self, user: OciUser, filepath, _min_gap, _max_gap) -> None:
        self._user = user
        self._client = ComputeClient(config=dict(user))
        self.tf = FileParser(filepath)
        self._min_gap = _min_gap
        self._max_gap = _max_gap
        self._gap_step = (_max_gap - _min_gap) / 10
        self.sleep_time = _min_gap

    def gen_pwd(self):
        passwd = ''.join(
            random.sample(
                'ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba#@1234567890',
                13))
        logger.info(f"创建ssh登陆密码:{passwd}\n")
        self._pwd = passwd
        sh = '#!/bin/bash \n    echo root:' + passwd + " | sudo chpasswd root\n    sudo sed -i 's/^.*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config;\n    sudo sed -i 's/^.*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config;\n    sudo reboot"
        sh64 = base64.b64encode(sh.encode('utf-8'))
        sh64 = str(sh64, 'utf-8')
        self._slcmd = sh64

    def create(self):
        # logger.info("与运行创建活动")
        # 开启一个tg的原始推送
        text = "脚本开始启动:\n,区域:{}-实例:{},CPU:{}C-内存:{}G-硬盘:{}G的小🐔已经快马加鞭抢购了\n".format(
            self.tf.availability_domain, self.tf.display_name, self.tf.ocpus,
            self.tf.memory_in_gbs, self.tf.boot_volume_size_in_gbs)
        telegram(text)
        self.gen_pwd()
        while True:
            try:
                ins = self.lunch_instance()  # 应该返回具体的成功的数据
            except oci.exceptions.ServiceError as e:
                if e.status == 429 and e.code == 'TooManyRequests' and e.message == 'Too many requests for the user':
                    # 被限速了，改一下时间
                    if self.sleep_time + self._gap_step <= self._max_gap:
                        self.sleep_time += self._gap_step
                    logger.info(f"请求太快了，自动调整请求时间: {self.sleep_time}秒")
                elif e.status == 502 and e.code == 'InternalError' and e.message == 'Bad Gateway':
                    logger.info(f"Bad Gateway, ignore: {e}\n")
                elif not (e.status == 500 and e.code == 'InternalError'
                          and e.message == 'Out of host capacity.'):
                    if "Service limit" in e.message and e.status == 400:

                        # 可能是别的错误，也有可能是 达到上限了，要去查看一下是否开通成功，也有可能错误了
                        self.logp("❌如果看到这条推送,说明刷到机器，但是开通失败了，请后台检查你的cpu，内存，硬盘占用情况，并释放对应的资源 返回值:{},\n 脚本停止".format(e))
                    else:
                        self.logp("❌发生错误,脚本停止!请检查参数或github反馈/查找 相关问题:{}".format(e))
                    telegram(self.desp)
                    raise e
                else:
                    if self.sleep_time >= self._min_gap + self._gap_step:
                        # 没有被限速，恢复减少的时间
                        self.sleep_time -= self._gap_step
                        logger.info(f"目前没有请求限速,快马加刷中: {self.sleep_time}")
                logger.info(f"本次返回信息: {e}\n")
                time.sleep(self.sleep_time)
            except (oci.exceptions.RequestException, oci.exceptions.ConnectTimeout) as e:
                logger.info(f"Exception occurred, ignore: {e}\n")
                time.sleep(self.sleep_time)
            else:
                #  开通成功 ，ins 就是返回的数据
                #  可以等一会去请求实例的ip
                # logger.info("开通成功之后的ins:\n\n", ins, type(ins))
                self.logp(
                    "🎉经过 {} 尝试后\n 区域:{}实例:{}-CPU:{}C-内存:{}G🐔创建成功了🎉\n".format(
                        self.try_count + 1,
                        self.tf.availability_domain,
                        self.tf.display_name,
                        self.tf.ocpus,
                        self.tf.memory_in_gbs,
                    ))
                self.ins_id = ins.id
                self.logp("ssh登陆密码: {} \n".format(self._pwd))
                self.check_public_ip()

                telegram(self.desp)
                break
            finally:
                self.try_count += 1
                logger.info(f"抢注中，已经经过:{self.try_count}尝试")

    def check_public_ip(self):
        network_client = VirtualNetworkClient(config=dict(self._user))
        count = 100
        while count:
            attachments = self._client.list_vnic_attachments(
                compartment_id=self._user.compartment_id(),
                instance_id=self.ins_id)
            data = attachments.data
            if len(data) != 0:
                logger.info("开始查找vnic id ")
                vnic_id = data[0].vnic_id
                public_ip = network_client.get_vnic(vnic_id).data.public_ip
                self.logp("公网ip为:{}\n 🐢脚本停止，感谢使用😄\n".format(public_ip))
                self.public_ip = public_ip
                return
            time.sleep(5)
            count -= 1
        self.logp("开机失败，被他娘甲骨文给关掉了😠，脚本停止，请重新运行\n")

    def lunch_instance(self):
        return self._client.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                display_name=self.tf.display_name,
                compartment_id=self.tf.compoartment_id,
                shape=self.shape,
                extended_metadata={'user_data': self._slcmd},
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.tf.ocpus, memory_in_gbs=self.tf.memory_in_gbs),
                availability_domain=self.tf.availability_domain,
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=self.tf.subnet_id,
                    hostname_label=self.tf.display_name),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=self.tf.image_id,
                    boot_volume_size_in_gbs=self.tf.boot_volume_size_in_gbs,
                ),
                metadata=dict(ssh_authorized_keys=self.tf.ssh_authorized_keys),
                is_pv_encryption_in_transit_enabled=True,
            )).data

    def logp(self, text):
        logger.info(text)
        if config['telegram']['enable']:
            self.desp += text


def init_logger():
    fh = logging.FileHandler("./log.txt", mode='w')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    _logger = logging.getLogger("OCI")
    _logger.setLevel(logging.DEBUG)
    _logger.addHandler(fh)
    _logger.addHandler(ch)

    return _logger


def init_config():
    with open("config.yaml", "r") as yaml_file:
        _config = yaml.load(yaml_file, Loader=yaml.FullLoader)

    return _config


if __name__ == "__main__":
    logger = init_logger()
    config = init_config()
    user = OciUser()
    path = sys.argv[1]
    ins = InsCreate(user, path, config['request']['min_gap'], config['request']['max_gap'])
    ins.create()
