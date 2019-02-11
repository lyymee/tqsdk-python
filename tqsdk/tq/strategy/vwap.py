#!/usr/bin/env python
#  -*- coding: utf-8 -*-
__author__ = 'limin'

import datetime
from tqsdk import TargetPosTask
from tqsdk.tq.strategy.base import StrategyBase


class StrategyVWAP(StrategyBase):
    def __init__(self, api, desc, stg_id, desc_chan):
        StrategyBase.__init__(self, api, desc, stg_id, desc_chan)
        self.add_input("合约代码", "symbol", "DCE.jd1905", str)
        self.add_input("时间单元", "time_cell", 5*60, int)
        self.add_input("目标手数", "target_volume", 300, int)
        self.add_input("历史数据天数", "history_day_length", 20, int)
        self.add_input("时间跨度", "time_span", 3600, int)
        self.add_switch()
        self.add_console()
        self.set_status()
        self.show()

    def get_desc(self):
        return "合约代码 %s, 时间单元 %d 秒, 目标手数 %d, 历史数据天数 %d, 时间跨度 %d 秒" % \
               (self.symbol, self.time_cell, self.target_volume, self.history_day_length, self.time_span)

    async def run_strategy(self):
        time_slot_start = datetime.datetime.now().time()  # 计划交易时段起始时间点
        time_slot_end = (datetime.datetime.now() + datetime.timedelta(0,self.time_span)).time()  # 计划交易时段终点时间点

        # 根据 history_day_length 推算出需要订阅的历史数据长度, 需要注意history_day_length与time_cell的比例关系以避免超过订阅限制
        klines = self.api.get_kline_serial(self.symbol, self.time_cell, data_length=int(10*60*60/self.time_cell*self.history_day_length))
        position = self.api.get_position(self.symbol)  # 持仓信息
        target_pos = TargetPosTask(self.api, self.symbol)

        try:
            async with self.api.register_update_notify() as update_chan:
                while not klines.is_ready():  # 等待数据
                    await update_chan.recv()

                df = klines.to_dataframe()  # 将k线数据转为DataFrame
                # 添加辅助列: time及date, 分别为K线时间的时:分:秒和其所属的交易日
                df["time"] = df.datetime.apply(lambda x: self.get_kline_time(x))
                df["date"] = df.datetime.apply(lambda x: self.get_market_day(x))

                # 获取在预设交易时间段内的所有K线, 即时间位于 time_slot_start 到 time_slot_end 之间的数据
                if time_slot_end > time_slot_start:  # 判断是否类似 23:00:00 开始， 01:00:00 结束这样跨天的情况
                    df = df[(df["time"] >= time_slot_start) & (df["time"] <= time_slot_end)]
                else:
                    df = df[(df["time"] >= time_slot_start) | (df["time"] <= time_slot_end)]

                # 由于可能有节假日导致部分天并没有填满整个预设交易时间段
                # 因此去除缺失部分交易时段的日期(即剩下的每个日期都包含预设的交易时间段内所需的全部时间单元)
                date_cnt = df["date"].value_counts()
                max_num = date_cnt.max()  # 所有日期中最完整的交易时段长度
                need_date = date_cnt[date_cnt == max_num].sort_index().index[-self.history_day_length - 1:-1]  # 获取今天以前的预设数目个交易日的日期
                df = df[df["date"].isin(need_date)]  # 最终用来计算的k线数据

                # 计算每个时间单元的成交量占比, 并使用算数平均计算出预测值
                datetime_grouped = df.groupby(['date', 'time'])['volume'].sum()  # 将K线的volume按照date、time建立多重索引分组
                # 计算每个交易日内的预设交易时间段内的成交量总和(level=0: 表示按第一级索引"data"来分组)后,将每根k线的成交量除以所在交易日内的总成交量,计算其所占比例
                volume_percent = datetime_grouped / datetime_grouped.groupby(level=0).sum()
                predicted_percent = volume_percent.groupby(level=1).mean()  # 将历史上相同时间单元的成交量占比使用算数平均计算出预测值
                print("各时间单元成交量占比:\n", predicted_percent)

                # 计算每个时间单元的成交量预测值
                predicted_volume = {}  # 记录每个时间单元需调整的持仓量
                percentage_left = 1  # 剩余比例
                volume_left = self.target_volume  # 剩余手数
                for index, value in predicted_percent.items():
                    volume = round(volume_left*(value/percentage_left))
                    predicted_volume[index] = volume
                    percentage_left -= value
                    volume_left -= volume
                print("\n各时间单元应下单手数:\n", predicted_volume)


                # 交易
                current_volume = 0  # 记录已调整持仓量
                async for _ in update_chan:
                    # 新产生一根K线并且在计划交易时间段内: 调整目标持仓量
                    if self.api.is_changing(klines[-1], "datetime"):
                        t = datetime.datetime.fromtimestamp(klines[-1]["datetime"]//1000000000).time()
                        if t in predicted_volume:
                            current_volume += predicted_volume[t]
                            print("到达下一时间单元,调整持仓为:", current_volume)
                            target_pos.set_target_volume(current_volume)
                    # 用持仓信息判断是否完成所有目标交易手数
                    if self.api.is_changing(position, "volume_long") or self.api.is_changing(position, "volume_short"):
                        if position["volume_long"] - position["volume_short"] == self.target_volume:
                            break
        finally:
            target_pos.task.cancel()


    def get_kline_time(self, kline_datetime):
        """获取k线的时间(不包含日期)"""
        kline_time = datetime.datetime.fromtimestamp(kline_datetime//1000000000).time()  # 每根k线的时间
        return kline_time

    def get_market_day(self, kline_datetime):
        """获取k线所对应的交易日"""
        kline_dt = datetime.datetime.fromtimestamp(kline_datetime//1000000000)  # 每根k线的日期和时间
        if kline_dt.hour >= 18:  # 当天18点以后: 移到下一个交易日
            kline_dt = kline_dt + datetime.timedelta(days=1)
        while kline_dt.weekday() >= 5:  # 是周六或周日,移到周一
            kline_dt = kline_dt + datetime.timedelta(days=1)
        return kline_dt.date()